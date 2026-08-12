import hashlib
import json
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.yandex.crypto import CredentialConfigurationError

from .client import TopvisorClient, TopvisorError, client_for_project
from .models import TopvisorProjectMapping, TopvisorSyncRun


def response_checksum(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_depth(raw) -> int:
    from apps.reports.calculations import normalize_ranking_depth

    return normalize_ranking_depth(raw)


def configuration_id(configuration):
    """Keep Topvisor's concrete variant ID, rather than collapsing device variants."""
    value = configuration.get("id") or configuration.get("searcher_id")
    if value is None:
        raise ValueError("Topvisor configuration has no stable identifier")
    return str(value)


def configuration_segment(configuration):
    """Return the report dimension, deliberately excluding the device."""
    engine = str(configuration.get("search_engine", configuration.get("searcher", "")))
    region = str(configuration.get("region_name", configuration.get("region", "")))
    return engine.strip().casefold(), " ".join(region.split()).casefold()


@transaction.atomic
def store_snapshot(*, mapping: TopvisorProjectMapping, configuration, snapshot_date: date, payload):
    """Idempotently replace a single immutable normalized API snapshot."""
    config_id = configuration_id(configuration)
    depth_raw = configuration.get("depth", configuration.get("check_depth"))
    depth = normalize_depth(depth_raw)
    rows = payload.get("positions", payload.get("rows", []))
    retrieved_at = timezone.now()
    defaults = {
        "search_engine": str(
            configuration.get("search_engine", configuration.get("searcher", ""))
        ).lower(),
        "region": str(configuration.get("region_name", configuration.get("region", ""))),
        "tracked_keyword_count": int(payload.get("tracked_keyword_count", len(rows))),
        "ranking_depth": depth,
        "depth_raw": str(depth_raw),
        "depth_retrieved_at": retrieved_at,
        "depth_source": RankingSnapshot.DepthSource.TOPVISOR_API,
        "visibility": Decimal(str(payload["visibility"]))
        if payload.get("visibility") is not None
        else None,
        "visibility_raw": payload.get("visibility"),
        "response_checksum": response_checksum(payload),
        "retrieved_at": retrieved_at,
        "provenance": {
            "method": "topvisor_api",
            "topvisor_project_id": mapping.topvisor_project_id,
            "configuration_id": config_id,
            "retrieved_at": retrieved_at.isoformat(),
        },
    }
    existing = RankingSnapshot.objects.filter(
        project=mapping.project,
        snapshot_date=snapshot_date,
        topvisor_configuration_id=config_id,
    ).first()
    unchanged = existing is not None and existing.response_checksum == defaults["response_checksum"]
    snapshot, created = RankingSnapshot.objects.update_or_create(
        project=mapping.project,
        snapshot_date=snapshot_date,
        topvisor_configuration_id=config_id,
        defaults=defaults,
    )
    if unchanged and snapshot.positions.exists():
        return snapshot, False
    snapshot.positions.all().delete()
    positions = []
    for row in rows:
        raw_position = row.get("position")
        position = (
            int(raw_position)
            if str(raw_position).isdigit() and int(raw_position) <= depth
            else None
        )
        query = str(row.get("query", row.get("name", ""))).strip()
        positions.append(
            KeywordPosition(
                ranking_snapshot=snapshot,
                query=query,
                normalized_query=" ".join(query.casefold().split()),
                frequency=int(row.get("frequency", row.get("ws"))),
                position_raw=str(raw_position or ""),
                position_value=position,
                position_status=KeywordPosition.Status.RANKED
                if position
                else KeywordPosition.Status.NOT_FOUND,
                group_name=str(row.get("group", row.get("group_name", "")) or ""),
                target_url=str(row.get("url", "") or ""),
                normalized_target_url=str(row.get("url", "") or ""),
            )
        )
    KeywordPosition.objects.bulk_create(positions, batch_size=1000)
    return snapshot, created


def available_projects(client=None):
    return tuple((client or TopvisorClient()).iter_projects())


def _month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def _shift_month(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def sync_positions(*, mapping, report_month, client=None):
    """Fetch the three-month reporting window and persist an auditable, idempotent result."""
    run = TopvisorSyncRun.objects.create(mapping=mapping, report_month=report_month)
    configurations = {configuration_id(item): item for item in mapping.selected_configurations}
    try:
        client = client or client_for_project(mapping.project)[0]
        segments = {}
        for configuration in configurations.values():
            segment = configuration_segment(configuration)
            if segment in segments:
                raise TopvisorError(
                    "Выбрано несколько конфигураций для одной поисковой системы и региона."
                )
            segments[segment] = configuration

        # Network access and validation deliberately happen before any ranking rows are changed.
        pending_snapshots = []
        for month in (_shift_month(report_month, -2), _shift_month(report_month, -1), report_month):
            snapshot_date = _month_end(month)
            for config_id, configuration in configurations.items():
                rows = list(
                    client.get_positions(
                        mapping.topvisor_project_id,
                        searcher_id=config_id,
                        date1=month.isoformat(),
                        date2=snapshot_date.isoformat(),
                        fields=["query", "position", "frequency", "group", "url"],
                    )
                )
                if any(row.get("frequency", row.get("ws")) is None for row in rows):
                    raise TopvisorError("В ответе Topvisor нет обязательной частотности.")
                payload = {"positions": rows, "tracked_keyword_count": len(rows)}
                # Validate depth before opening the write transaction as well.
                normalize_depth(configuration.get("depth", configuration.get("check_depth")))
                pending_snapshots.append((configuration, snapshot_date, payload))

        with transaction.atomic():
            for configuration, snapshot_date, payload in pending_snapshots:
                store_snapshot(
                    mapping=mapping,
                    configuration=configuration,
                    snapshot_date=snapshot_date,
                    payload=payload,
                )
            mapping.last_checked_at = timezone.now()
            mapping.save(update_fields=["last_checked_at", "updated_at"])

        run.status = TopvisorSyncRun.Status.SUCCESS
        run.loaded_keyword_count = sum(
            len(payload["positions"]) for _, _, payload in pending_snapshots
        )
        run.segments = [
            {
                "search_engine": str(
                    configuration.get("search_engine", configuration.get("searcher", ""))
                ),
                "region": str(configuration.get("region_name", configuration.get("region", ""))),
                "depth": normalize_depth(
                    configuration.get("depth", configuration.get("check_depth"))
                ),
            }
            for configuration in segments.values()
        ]
    except Exception as exc:
        run.status = TopvisorSyncRun.Status.FAILED
        if isinstance(exc, CredentialConfigurationError):
            run.error_message = (
                "Не удалось прочитать сохранённые реквизиты. Проверьте ключ шифрования "
                "или сохраните подключение заново"
            )
        elif isinstance(exc, TopvisorError):
            message = str(exc)
            credentials = getattr(client, "credentials", None)
            for secret in (
                getattr(credentials, "user_id", ""),
                getattr(credentials, "api_key", ""),
                settings.TOPVISOR_USER_ID,
                settings.TOPVISOR_API_KEY,
            ):
                if secret:
                    message = message.replace(secret, "[скрыто]")
            run.error_message = message[:500]
        else:
            run.error_message = "Не удалось синхронизировать данные Topvisor."
    run.completed_at = timezone.now()
    run.save(
        update_fields=[
            "status",
            "loaded_keyword_count",
            "segments",
            "error_message",
            "completed_at",
        ]
    )
    return run

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


def normalize_depth(raw, searcher="google") -> int:
    value = int(raw)
    if value in (10, 20, 30, 50, 100, 200, 300, 500, 1000):
        return value
    try:
        mapping = (
            {1: 100, 2: 200, 3: 300, 5: 500, 10: 1000}
            if "yandex" in str(searcher).casefold() or "яндекс" in str(searcher).casefold()
            else {1: 10, 2: 20, 3: 30, 5: 50, 10: 100}
        )
        return mapping[value]
    except KeyError as exc:
        raise ValueError("Unsupported ranking depth") from exc


def configuration_id(configuration):
    """Keep Topvisor's concrete variant ID, rather than collapsing device variants."""
    value = configuration.get("id")
    if value is None and configuration.get("searcher_id") is not None:
        value = f"{configuration['searcher_id']}:{configuration.get('region_id', '')}"
    if value is None:
        raise ValueError("Topvisor configuration has no stable identifier")
    return str(value)


def configuration_segment(configuration):
    """Return the report dimension, deliberately excluding the device."""
    engine = str(
        configuration.get(
            "searcher_name", configuration.get("search_engine", configuration.get("searcher", ""))
        )
    )
    region = str(configuration.get("region_name", configuration.get("region", "")))
    return engine.strip().casefold(), " ".join(region.split()).casefold()


@transaction.atomic
def store_snapshot(*, mapping: TopvisorProjectMapping, configuration, snapshot_date: date, payload):
    """Idempotently replace a single immutable normalized API snapshot."""
    config_id = configuration_id(configuration)
    depth_raw = configuration.get(
        "raw_depth", configuration.get("depth", configuration.get("check_depth"))
    )
    provider_depth = configuration.get("normalized_depth") or normalize_depth(
        depth_raw, configuration.get("searcher_name", configuration.get("search_engine", ""))
    )
    depth = min(provider_depth, 100)
    rows = payload.get("positions", payload.get("rows", []))
    retrieved_at = timezone.now()
    defaults = {
        "search_engine": str(
            configuration.get(
                "searcher_name",
                configuration.get("search_engine", configuration.get("searcher", "")),
            )
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
            "provider_depth": provider_depth,
            "report_depth": depth,
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
                frequency=int(row.get("frequency", row.get("ws")) or 0),
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


def _history_rows(payload, configuration, project_id):
    """Normalize Topvisor's documented headers/positionsData history shape."""
    dates = (payload.get("headers") or {}).get("dates") or payload.get("existsDates") or []
    dates = [str(item.get("date", item)) if isinstance(item, dict) else str(item) for item in dates]
    region_index = str(configuration.get("region_index", ""))
    volume_key = f"volume:{configuration.get('region_key')}:{configuration.get('searcher_key')}:1"
    result = {value: [] for value in dates}
    for keyword in payload.get("keywords", []):
        positions = keyword.get("positionsData") or {}
        for value in dates:
            position_data = positions.get(f"{value}:{project_id}:{region_index}")
            if position_data is None:
                position_data = {}
            if not isinstance(position_data, dict):
                position_data = {"position": position_data}
            result[value].append(
                {
                    "query": keyword.get("name", keyword.get("query", "")),
                    "frequency": keyword.get(volume_key, keyword.get("frequency", 0)),
                    "group": keyword.get("group_name", keyword.get("group", "")),
                    "position": position_data.get("position", position_data.get("pos", "")),
                    "url": position_data.get("relevant_url", position_data.get("url", "")),
                }
            )
    return result


def sync_positions(*, mapping, report_month=None, client=None):
    """Download existing checks only; this never starts a provider position check."""
    explicit_report_month = report_month is not None
    report_month = report_month or timezone.localdate().replace(day=1)
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

        # All pages are downloaded before opening the atomic write transaction.
        pending_snapshots = []
        for _config_id, configuration in configurations.items():
            volume = (
                f"volume:{configuration.get('region_key')}:{configuration.get('searcher_key')}:1"
            )
            if hasattr(client, "get_position_history"):
                pages = list(
                    client.get_position_history(
                        mapping.topvisor_project_id,
                        regions_indexes=[str(configuration["region_index"])],
                        fields=["name", "group_name", volume],
                        positions_fields=["position", "relevant_url"],
                    )
                )
                combined = {}
                for page in pages:
                    for snapshot_date, rows in _history_rows(
                        page, configuration, str(mapping.topvisor_project_id)
                    ).items():
                        combined.setdefault(snapshot_date, []).extend(rows)
                for snapshot_date, rows in combined.items():
                    pending_snapshots.append(
                        (
                            configuration,
                            date.fromisoformat(snapshot_date),
                            {"positions": rows, "tracked_keyword_count": len(rows)},
                        )
                    )
            else:  # compatibility with older adapters
                months = (
                    (_shift_month(report_month, -2), _shift_month(report_month, -1), report_month)
                    if explicit_report_month
                    else (report_month,)
                )
                for month in months:
                    rows = list(
                        client.get_positions(
                            mapping.topvisor_project_id,
                            regions_indexes=[str(configuration.get("region_index", ""))],
                        )
                    )
                    if any(row.get("frequency", row.get("ws")) is None for row in rows):
                        raise TopvisorError("В ответе Topvisor нет обязательной частотности.")
                    pending_snapshots.append(
                        (
                            configuration,
                            _month_end(month),
                            {"positions": rows, "tracked_keyword_count": len(rows)},
                        )
                    )

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
                    configuration.get(
                        "searcher_name",
                        configuration.get("search_engine", configuration.get("searcher", "")),
                    )
                ),
                "region": str(configuration.get("region_name", configuration.get("region", ""))),
                "depth": min(
                    normalize_depth(
                        configuration.get(
                            "normalized_depth",
                            configuration.get("depth", configuration.get("check_depth")),
                        ),
                        configuration.get("searcher_name", configuration.get("search_engine", "")),
                    ),
                    100,
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

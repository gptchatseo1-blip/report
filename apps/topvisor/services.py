import hashlib
import json
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.metrics.normalization import normalize_frequency
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
                frequency=normalize_frequency(row.get("frequency", row.get("ws"))),
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


def _normalized_query(value):
    return " ".join(str(value or "").casefold().split())


def _volume_aliases(payload):
    """Map response keyword keys to real volume fields declared by Topvisor headers."""
    headers = payload.get("headers") or {}
    fields = headers.get("fields", payload.get("fields", [])) or []
    labels = headers.get("fieldsLabels", payload.get("fieldsLabels", {})) or {}
    aliases = {}

    def register(alias, field):
        alias, field = str(alias), str(field)
        if re.fullmatch(r"volume:[^:]+:[^:]+:1", field):
            aliases[alias] = field

    if isinstance(fields, dict):
        for alias, field in fields.items():
            register(alias, field)
            register(field, alias)
    else:
        for field in fields:
            if isinstance(field, dict):
                canonical = field.get("name", field.get("field", field.get("code", "")))
                register(field.get("alias", canonical), canonical)
            else:
                register(field, field)
    if isinstance(labels, dict):
        for alias, field in labels.items():
            register(alias, field)
            register(field, alias)
    for keyword in payload.get("keywords", []):
        for key in keyword:
            register(key, key)
    return aliases


def _frequency_candidates(payload, yandex_volume_fields):
    aliases = _volume_aliases(payload)
    preferred = [alias for alias, field in aliases.items() if field in yandex_volume_fields]
    return preferred + [alias for alias in aliases if alias not in preferred]


def _history_rows(payload, configuration, project_id, frequency_map):
    """Normalize Topvisor's documented headers/positionsData history shape."""
    dates = (payload.get("headers") or {}).get("dates") or payload.get("existsDates") or []
    dates = [str(item.get("date", item)) if isinstance(item, dict) else str(item) for item in dates]
    region_index = str(configuration.get("region_index", ""))
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
                    "frequency": frequency_map.get(
                        _normalized_query(keyword.get("name", keyword.get("query", "")))
                    ),
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

        # Every response page is downloaded before validation and the atomic write.
        pending_snapshots = []
        if hasattr(client, "get_position_history"):
            yandex_volumes = {
                f"volume:{item.get('region_key')}:{item.get('searcher_key')}:1"
                for item in configurations.values()
                if "yandex"
                in str(item.get("searcher_name", item.get("search_engine", ""))).casefold()
                or "яндекс"
                in str(item.get("searcher_name", item.get("search_engine", ""))).casefold()
            }
            fallback_volumes = [
                f"volume:{item.get('region_key')}:{item.get('searcher_key')}:1"
                for item in configurations.values()
                if item.get("region_key") is not None and item.get("searcher_key") is not None
            ]
            # Frequency belongs to the keyword, not to every position segment. Asking
            # Google-specific volume fields can make Topvisor reject an otherwise valid request.
            requested_volumes = sorted(yandex_volumes) or fallback_volumes[:1]
            downloaded = []
            for configuration in configurations.values():
                common = {
                    "regions_indexes": [str(configuration["region_index"])],
                    "fields": ["name", "group_name", *requested_volumes],
                    "positions_fields": ["position", "relevant_url"],
                }
                existing_dates = client.get_existing_position_dates(
                    mapping.topvisor_project_id, **common
                )
                pages = []
                for start in range(0, len(existing_dates), 20):
                    pages.extend(
                        client.get_position_history(
                            mapping.topvisor_project_id,
                            dates=list(existing_dates[start : start + 20]),
                            **common,
                        )
                    )
                downloaded.append((configuration, tuple(existing_dates), pages))

            frequency_map = {}
            all_queries = set()
            for _configuration, _dates, pages in downloaded:
                for page in pages:
                    candidates = _frequency_candidates(page, yandex_volumes)
                    for keyword in page.get("keywords", []):
                        query = _normalized_query(keyword.get("name", keyword.get("query", "")))
                        all_queries.add(query)
                        if query in frequency_map:
                            continue
                        for alias in candidates:
                            value = keyword.get(alias)
                            if value is None or str(value).strip() == "":
                                continue
                            try:
                                frequency_map[query] = normalize_frequency(value)
                            except ValueError:
                                raise TopvisorError(
                                    "В ответе Topvisor недопустимая частотность."
                                ) from None
                            break
            missing_count = len(all_queries - frequency_map.keys())
            if missing_count:
                raise TopvisorError(
                    f"В ответе Topvisor нет проверенной частотности запросов: {missing_count}."
                )

            for configuration, existing_dates, pages in downloaded:
                combined = {}
                for page in pages:
                    for snapshot_date, rows in _history_rows(
                        page, configuration, str(mapping.topvisor_project_id), frequency_map
                    ).items():
                        combined.setdefault(snapshot_date, []).extend(rows)
                if set(combined) != set(existing_dates):
                    raise TopvisorError("Не удалось полностью загрузить все даты Topvisor.")
                for snapshot_date, rows in combined.items():
                    pending_snapshots.append(
                        (
                            configuration,
                            date.fromisoformat(snapshot_date),
                            {"positions": rows, "tracked_keyword_count": len(rows)},
                        )
                    )
        else:  # compatibility with older adapters
            for configuration in configurations.values():
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
                    try:
                        for row in rows:
                            key = "frequency" if "frequency" in row else "ws"
                            row["frequency"] = normalize_frequency(row.get(key))
                    except ValueError:
                        raise TopvisorError("В ответе Topvisor недопустимая частотность.") from None
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

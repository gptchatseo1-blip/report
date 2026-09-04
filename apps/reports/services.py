import base64
import hashlib
import io
import json
import logging
import re
import urllib.request
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from PIL import Image

from apps.metrics.models import MetricPoint, RankingSnapshot, SourceSnapshot
from apps.worklog.models import WorkLogItem

from .calculations import (
    FORMULA_VERSION,
    ChangeKind,
    PositionItem,
    calculate_change,
    calculate_periods,
    calculate_position_distribution,
    calculate_source_shares,
    check_ctr,
    compare_semantics,
    depth_comment,
    shift_month,
    top_11_20_rows,
)
from .models import (
    GeneratedArtifact,
    Report,
    ReportDatasetSnapshot,
    ReportVersion,
    ValidationIssue,
)

SNAPSHOT_SCHEMA_VERSION = "mvp1.1"
logger = logging.getLogger(__name__)
SENSITIVE_SOURCE_KEY_RE = re.compile(
    r"(?i)^(?:api[_ -]?key|authorization|oauth[_ -]?token|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|password|cookie|set[_ -]?cookie)$"
)
SENSITIVE_SOURCE_TEXT_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer|basic|oauth)\s+\S+|"
    r"sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,})"
)


def redact_sensitive_source_data(value):
    """Remove credentials accidentally retained in legacy provider payloads."""
    if isinstance(value, dict):
        return {
            str(key): (
                "redacted"
                if SENSITIVE_SOURCE_KEY_RE.fullmatch(str(key).strip())
                else redact_sensitive_source_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_sensitive_source_data(item) for item in value]
    if isinstance(value, str) and SENSITIVE_SOURCE_TEXT_RE.search(value):
        return "redacted"
    return value


def _project_favicon(domain):
    """Freeze a small public favicon in the snapshot; test domains never trigger I/O."""
    if not getattr(settings, "REPORT_FAVICON_FETCH_ENABLED", True):
        return None
    domain = str(domain or "").strip().casefold()
    if not domain or domain.endswith((".example", ".test", ".invalid", ".localhost")):
        return None
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=32"
    request = urllib.request.Request(url, headers={"User-Agent": "SEO-Report/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(128 * 1024 + 1)
        if not raw or len(raw) > 128 * 1024:
            return None
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            icon = source.convert("RGBA")
            output = io.BytesIO()
            icon.save(output, format="PNG")
        return {"mime_type": "image/png", "data": base64.b64encode(output.getvalue()).decode()}
    except (OSError, ValueError):
        logger.info("Project favicon was not available for %s", domain)
        return None


class ReportVersionDeleteBlocked(Exception):
    """Raised when deleting a version would race with an active export."""


def _delete_artifact_file(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception("Failed to remove artifact file after report version deletion")


def build_position_facts(*, project, report_month, selected_dates=None):
    """Build facts independently for every search-engine/region pair; device is absent by design."""
    periods = calculate_periods(report_month)
    snapshot_filter = {"project": project}
    selected_by_engine = selected_dates if isinstance(selected_dates, dict) else None
    flat_dates = {day for dates in (selected_by_engine or {}).values() for day in dates}
    if selected_dates:
        snapshot_filter["snapshot_date__in"] = flat_dates if selected_by_engine else selected_dates
    else:
        snapshot_filter["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**snapshot_filter)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    grouped = defaultdict(dict)
    grouped_daily = defaultdict(dict)
    chart_snapshots = (
        RankingSnapshot.objects.filter(
            project=project,
            snapshot_date__range=(periods.three_months.start, periods.report.end),
        )
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
        if selected_dates
        else snapshots
    )
    for snapshot in chart_snapshots:
        segment_key = (snapshot.search_engine, snapshot.region, snapshot.topvisor_configuration_id)
        daily_existing = grouped_daily[segment_key].get(snapshot.snapshot_date)
        daily_existing_key = (
            (daily_existing.created_at, str(daily_existing.id)) if daily_existing else None
        )
        if (
            daily_existing_key is None
            or (snapshot.created_at, str(snapshot.id)) > daily_existing_key
        ):
            grouped_daily[segment_key][snapshot.snapshot_date] = snapshot
    for snapshot in snapshots:
        if selected_by_engine and snapshot.snapshot_date not in selected_by_engine.get(
            snapshot.search_engine, ()
        ):
            continue
        month = snapshot.snapshot_date if selected_dates else snapshot.snapshot_date.replace(day=1)
        # The latest snapshot inside a calendar month wins deterministically.
        segment_key = (snapshot.search_engine, snapshot.region, snapshot.topvisor_configuration_id)
        existing = grouped[segment_key].get(month)
        snapshot_key = (snapshot.snapshot_date, snapshot.created_at, str(snapshot.id))
        existing_key = (
            (existing.snapshot_date, existing.created_at, str(existing.id)) if existing else None
        )
        if existing_key is None or snapshot_key > existing_key:
            grouped[segment_key][month] = snapshot
    facts = []
    engine_order = {"yandex": 0, "google": 1}
    for (engine, region, configuration_id), values in sorted(
        grouped.items(), key=lambda item: (engine_order.get(item[0][0], 99), item[0][1], item[0][2])
    ):
        months = (
            tuple(selected_by_engine.get(engine, ()))
            if selected_by_engine
            else (
                tuple(selected_dates)
                if selected_dates
                else tuple(shift_month(periods.report.start, offset) for offset in (-2, -1, 0))
            )
        )
        report_snapshot = values.get(months[-1])
        previous_snapshot = values.get(months[0] if selected_dates else periods.previous.start)
        report_rows = tuple(report_snapshot.positions.all()) if report_snapshot else ()
        previous_rows = tuple(previous_snapshot.positions.all()) if previous_snapshot else ()
        comparison_depth = (
            min(report_snapshot.ranking_depth, previous_snapshot.ranking_depth)
            if report_snapshot and previous_snapshot
            else None
        )
        distribution = calculate_position_distribution(
            (
                PositionItem(
                    row.normalized_query,
                    row.frequency,
                    row.position_value,
                    row.group_name,
                    row.normalized_target_url,
                )
                for row in report_rows
            ),
            ranking_depth=report_snapshot.ranking_depth if report_snapshot else 100,
        )
        monthly_series = []
        for month in months:
            snapshot = values.get(month)
            if snapshot is None:
                continue
            monthly_series.append(
                {
                    "month": month,
                    "visibility": snapshot.visibility,
                    "distribution": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in snapshot.positions.all()
                        ),
                        ranking_depth=snapshot.ranking_depth,
                    ),
                    "ranking_depth": snapshot.ranking_depth,
                }
            )
        chart_series = []
        for snapshot_day, snapshot in sorted(
            grouped_daily[(engine, region, configuration_id)].items()
        ):
            chart_series.append(
                {
                    "month": snapshot_day,
                    "visibility": snapshot.visibility,
                    "distribution": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in snapshot.positions.all()
                        ),
                        ranking_depth=snapshot.ranking_depth,
                    ),
                    "ranking_depth": snapshot.ranking_depth,
                }
            )
        facts.append(
            {
                "search_engine": engine,
                "region": region,
                "configuration_id": configuration_id,
                "distribution": distribution,
                "visibility_change": calculate_change(
                    report_snapshot.visibility if report_snapshot else None,
                    previous_snapshot.visibility if previous_snapshot else None,
                    kind=ChangeKind.PERCENTAGE_POINTS,
                ),
                "ranking_depth": report_snapshot.ranking_depth if report_snapshot else None,
                "depth_comment": depth_comment(report_snapshot.ranking_depth)
                if engine == "google" and report_snapshot
                else None,
                "comparison_depth": comparison_depth,
                "comparison_distributions": {
                    "previous": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in previous_rows
                        ),
                        ranking_depth=comparison_depth,
                    ),
                    "current": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in report_rows
                        ),
                        ranking_depth=comparison_depth,
                    ),
                }
                if comparison_depth
                else None,
                "warnings": (
                    {
                        "code": "ranking_depth_changed",
                        "previous_depth": previous_snapshot.ranking_depth,
                        "current_depth": report_snapshot.ranking_depth,
                        "visibility_comparable": False,
                    },
                )
                if report_snapshot
                and previous_snapshot
                and report_snapshot.ranking_depth != previous_snapshot.ranking_depth
                else (),
                "three_month_series": tuple(monthly_series),
                "chart_series": tuple(chart_series),
                "semantics": compare_semantics(
                    (row.normalized_query for row in previous_rows),
                    (row.normalized_query for row in report_rows),
                ),
                "top_11_20": top_11_20_rows(
                    (
                        PositionItem(
                            row.normalized_query,
                            row.frequency,
                            row.position_value,
                            row.group_name,
                            row.normalized_target_url,
                        )
                        for row in report_rows
                    ),
                    depth=report_snapshot.ranking_depth if report_snapshot else 0,
                    mode=project.top_11_20_mode,
                ),
            }
        )
    return {"formula_version": FORMULA_VERSION, "periods": periods, "segments": facts}


def build_source_facts(*, project, report_month, selected_snapshot_ids=None, display_options=None):
    """Calculate each source exclusively from its independently selected snapshots."""
    periods = calculate_periods(report_month)
    selected_snapshot_ids = selected_snapshot_ids or {}
    result = {}
    for source in (SourceSnapshot.Source.METRIKA, SourceSnapshot.Source.WEBMASTER):
        ids = selected_snapshot_ids.get(source)
        if ids is None:  # Backward compatibility for old/programmatic snapshots only.
            rows = SourceSnapshot.objects.filter(
                project=project,
                source=source,
                period_start__range=(periods.three_months.start, periods.report.start),
            )
        else:
            rows = SourceSnapshot.objects.filter(project=project, source=source, id__in=ids)
        snapshots = list(
            rows.prefetch_related("metrics").order_by("period_start", "period_end", "id")
        )
        points = []
        all_traffic_totals = []
        options = display_options or {}
        segment = "search" if options.get("metrika_search_segment", True) else "all"
        robotness = options.get("metrika_robotness", "humans")
        prefix = f"segment_{segment}_{robotness}_"
        for snapshot in snapshots:
            raw_metrics = {point.metric_code: point for point in snapshot.metrics.all()}
            all_traffic_totals.append(raw_metrics.get("visits"))
            metrics = {
                code: point
                for code, point in raw_metrics.items()
                if not code.startswith("segment_")
            }
            if source == SourceSnapshot.Source.METRIKA:
                metrics.update(
                    {
                        code.removeprefix(prefix): point
                        for code, point in raw_metrics.items()
                        if code.startswith(prefix)
                    }
                )
            points.append((snapshot, metrics))
        # One point has no comparison period: do not manufacture a zero change.
        first = points[0][1] if len(points) >= 2 else {}
        current = points[-1][1] if points else {}

        def monthly_total(point, snapshot):
            if point is None:
                return None
            # Metrika and Webmaster snapshots already contain totals for the
            # selected calendar month. Report comparisons must use those totals
            # directly; dividing by the number of days obscures the figures that
            # users see in the provider interfaces.
            return point.numeric_value

        codes = sorted({code for _snapshot, metrics in points for code in metrics})
        series = {
            code: [
                {
                    "month": snapshot.period_start,
                    "value": monthly_total(metrics.get(code), snapshot),
                }
                for snapshot, metrics in points
            ]
            for code in codes
        }
        changes = {}
        for code in sorted(set(first) | set(current)):
            old, new = first.get(code), current.get(code)
            unit = (new or old).unit
            kind = (
                ChangeKind.PERCENTAGE_POINTS
                if unit == MetricPoint.Unit.PERCENT
                else ChangeKind.VALUE
            )
            changes[code] = calculate_change(
                monthly_total(new, points[-1][0]) if points else None,
                monthly_total(old, points[0][0]) if points else None,
                kind=kind,
            )
        extra = {}
        extra["period_details"] = [
            {
                "period_start": snapshot.period_start,
                "period_end": snapshot.period_end,
                "payload": redact_sensitive_source_data(snapshot.payload),
            }
            for snapshot, _metrics in points
        ]
        if source == SourceSnapshot.Source.METRIKA:
            source_api_total = None
            if snapshots:
                raw_total = (snapshots[-1].payload.get("traffic_source_total") or {}).get("visits")
                try:
                    source_api_total = Decimal(str(raw_total)) if raw_total is not None else None
                except (ArithmeticError, ValueError):
                    source_api_total = None
            all_traffic_total = (
                source_api_total
                if source_api_total is not None
                else (
                    all_traffic_totals[-1].numeric_value
                    if all_traffic_totals and all_traffic_totals[-1]
                    else None
                )
            )
            sources = {
                code.removeprefix("source_").removesuffix("_visits"): point.numeric_value
                for code, point in current.items()
                if code.startswith("source_")
            }
            extra["traffic_sources"] = calculate_source_shares(all_traffic_total, sources)
            extra["traffic_source_series"] = {
                code.removeprefix("source_").removesuffix("_visits"): series[code]
                for code in codes
                if code.startswith("source_")
            }
            extra["traffic_source_dynamics"] = {}
            for name, values in extra["traffic_source_series"].items():
                raw = sources.get(name)
                extra["traffic_source_dynamics"][name] = {
                    "series": values,
                    "share_percent": raw * Decimal("100") / all_traffic_total
                    if raw is not None and all_traffic_total not in (None, 0)
                    else None,
                    "change": calculate_change(
                        values[-1]["value"] if values else None,
                        values[0]["value"] if values else None,
                        kind=ChangeKind.VALUE,
                    ),
                }
        else:
            extra["ctr_check"] = check_ctr(
                current["search_clicks"].numeric_value if "search_clicks" in current else None,
                current["search_impressions"].numeric_value
                if "search_impressions" in current
                else None,
                current["search_ctr"].numeric_value if "search_ctr" in current else None,
            )
        result[source] = {"normalized_changes": changes, "three_month_series": series, **extra}
    return {"formula_version": FORMULA_VERSION, "periods": periods, "sources": result}


def _json_value(value):
    """Turn calculation dataclasses into a stable, JSON-compatible value."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical_json(payload):
    return json.dumps(
        _json_value(payload),
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_checksum(payload):
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _ranking_source_data(project, periods, selected_dates=None):
    filters = {"project": project}
    selected_by_engine = selected_dates if isinstance(selected_dates, dict) else None
    if selected_dates:
        filters["snapshot_date__in"] = (
            {d for dates in selected_by_engine.values() for d in dates}
            if selected_by_engine
            else selected_dates
        )
    else:
        filters["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**filters)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    result = []
    for item in snapshots:
        if selected_by_engine and item.snapshot_date not in selected_by_engine.get(
            item.search_engine, ()
        ):
            continue
        result.append(
            {
                "id": str(item.id),
                "date": item.snapshot_date,
                "search_engine": item.search_engine,
                "region": item.region,
                "configuration_id": item.topvisor_configuration_id,
                "ranking_depth": item.ranking_depth,
                "depth_raw": item.depth_raw,
                "visibility": item.visibility,
                "visibility_raw": item.visibility_raw,
                "positions": [
                    {
                        "query": row.query,
                        "normalized_query": row.normalized_query,
                        "frequency": row.frequency,
                        "position": row.position_value,
                        "status": row.position_status,
                        "group": row.group_name,
                        "target_url": row.normalized_target_url,
                    }
                    for row in item.positions.order_by("normalized_query", "group_name", "id")
                ],
                "provenance": {
                    "method": item.depth_source,
                    "retrieved_at": item.retrieved_at,
                    "depth_retrieved_at": item.depth_retrieved_at,
                    "response_checksum": item.response_checksum,
                    "import_batch_id": str(item.import_batch_id) if item.import_batch_id else None,
                },
            }
        )
    return result


def _external_source_data(project, periods, selected_ids=None):
    filters = {"project": project}
    if selected_ids is not None:
        filters["id__in"] = selected_ids
    else:
        filters["period_start__range"] = (periods.three_months.start, periods.report.start)
    rows = (
        SourceSnapshot.objects.filter(**filters)
        .prefetch_related("metrics")
        .order_by("source", "period_start", "period_end", "id")
    )
    return [
        {
            "id": str(row.id),
            "source": row.source,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "payload": redact_sensitive_source_data(row.payload),
            "metrics": [
                {
                    "code": point.metric_code,
                    "value": point.numeric_value,
                    "unit": point.unit,
                    "dimensions": point.dimensions,
                }
                for point in row.metrics.order_by("metric_code", "id")
            ],
            "retrieval_method": row.retrieval_method,
            "checksum": row.checksum,
            "retrieved_at": row.retrieved_at,
            "provenance": redact_sensitive_source_data(row.provenance),
            "sampling": row.sampling,
            "contains_sensitive_data": row.contains_sensitive_data,
        }
        for row in rows
    ]


def build_report_snapshot(*, report, selection=None):
    project = report.project
    periods = calculate_periods(report.report_month)
    works = (
        WorkLogItem.objects.filter(
            project=project, work_date__range=(periods.report.start, periods.report.end)
        )
        .select_related("category")
        .order_by("work_date", "category__sort_order", "title", "id")
    )
    explicit_selection = selection is not None
    selection = selection or {}
    raw_topvisor = selection.get("topvisor")
    if raw_topvisor is None:
        legacy = selection.get("topvisor_dates", ())
        selected_dates = tuple(date.fromisoformat(value) for value in legacy)
        selected_by_engine = None
    else:
        selected_by_engine = {
            engine: tuple(date.fromisoformat(value) for value in raw_topvisor.get(engine, ()))
            for engine in ("yandex", "google")
        }
        selected_dates = selected_by_engine
    selected_source_ids = tuple(selection.get("yandex_metrika", ())) + tuple(
        selection.get("yandex_webmaster", ())
    )
    display_options = (
        selection.get("display_options", {"show_urls": False})
        if explicit_selection
        else {"show_urls": True}
    )
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "project": {
            "id": str(project.id),
            "name": project.name,
            "domain": project.domain,
            "normalized_domain": project.normalized_domain,
            "timezone": project.timezone,
            "language": project.language,
            "top_11_20_mode": project.top_11_20_mode,
            "position_provider": project.position_provider,
            "favicon": _project_favicon(project.normalized_domain),
            "brand_rules": list(
                project.brand_rules.order_by("kind", "pattern", "priority", "id").values(
                    "kind", "pattern", "priority", "active"
                )
            ),
            "url_groups": [
                {
                    "name": group.name,
                    "slug": group.slug,
                    "priority": group.priority,
                    "active": group.active,
                    "rules": list(
                        group.rules.order_by("type", "pattern", "priority", "id").values(
                            "type", "pattern", "priority", "active"
                        )
                    ),
                }
                for group in project.url_groups.prefetch_related("rules").order_by(
                    "name", "slug", "priority", "id"
                )
            ],
            "provenance": {"method": "project_database", "updated_at": project.updated_at},
        },
        "periods": periods,
        "display_options": display_options,
        "source_selection": {
            "topvisor": (
                {
                    engine: {
                        "selected_dates": dates,
                        "comparison_start": dates[0] if dates else None,
                        "comparison_end": dates[-1] if dates else None,
                        "intermediate_dates": dates[1:-1],
                        "snapshots": [],
                    }
                    for engine, dates in selected_by_engine.items()
                }
                if selected_by_engine is not None
                else {
                    "selected_dates": selected_dates,
                    "comparison_start": selected_dates[0] if selected_dates else None,
                    "comparison_end": selected_dates[-1] if selected_dates else None,
                    "intermediate_dates": selected_dates[1:-1],
                }
            ),
            "yandex_metrika": list(selection.get("yandex_metrika", ())),
            "yandex_webmaster": list(selection.get("yandex_webmaster", ())),
        },
        "ranking_sources": _ranking_source_data(project, periods, selected_dates),
        "source_snapshots": _external_source_data(
            project, periods, selected_source_ids if explicit_selection else None
        ),
        "calculated": {
            "positions": build_position_facts(
                project=project, report_month=report.report_month, selected_dates=selected_dates
            ),
            "sources": build_source_facts(
                project=project,
                report_month=report.report_month,
                selected_snapshot_ids={
                    SourceSnapshot.Source.METRIKA: selection.get("yandex_metrika", []),
                    SourceSnapshot.Source.WEBMASTER: selection.get("yandex_webmaster", []),
                }
                if explicit_selection
                else None,
                display_options=display_options,
            ),
        },
        "completed_work": [
            {
                "date": item.work_date,
                "category": item.category.name,
                "title": item.title,
                "status": item.status,
                "url": item.url,
                "page_or_material_name": item.page_or_material_name,
                "character_count": item.character_count,
                "responsible": item.responsible,
                "comment": item.comment,
                "result_url": item.result_url,
                "provenance": {
                    "method": "worklog",
                    "id": str(item.id),
                    "updated_at": item.updated_at,
                },
            }
            for item in works
        ],
    }
    if selected_by_engine is not None:
        for engine in selected_by_engine:
            payload["source_selection"]["topvisor"][engine]["snapshots"] = [
                {
                    "identifier": source["id"],
                    "checksum": source["provenance"].get("response_checksum"),
                    "date": source["date"],
                    "search_engine": source["search_engine"],
                    "region": source["region"],
                    "configuration": source["configuration_id"],
                    "actual_depth": source["ranking_depth"],
                    "retrieved_at": source["provenance"].get("retrieved_at"),
                    "provenance": {"method": source["provenance"].get("method")},
                }
                for source in payload["ranking_sources"]
                if source["search_engine"] == engine
            ]
    return _json_value(payload)


@transaction.atomic
def create_report_version(*, report, created_by=None, selection=None):
    """Explicitly freeze current source data; no version is made by reads or source updates."""
    locked_report = Report.objects.select_for_update().select_related("project").get(pk=report.pk)
    number = (locked_report.versions.aggregate(value=Max("number"))["value"] or 0) + 1
    payload = build_report_snapshot(report=locked_report, selection=selection)
    version = ReportVersion.objects.create(
        report=locked_report, number=number, created_by=created_by
    )
    ReportDatasetSnapshot.objects.create(
        version=version,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        formula_version=FORMULA_VERSION,
        payload=payload,
        checksum=snapshot_checksum(payload),
    )
    issues = [
        ValidationIssue(
            version=version,
            code=warning["code"],
            section_code="position_dynamics",
            details=warning,
            message="Глубина проверки позиций изменилась относительно предыдущего месяца.",
        )
        for segment in payload["calculated"]["positions"]["segments"]
        for warning in segment["warnings"]
    ]
    ValidationIssue.objects.bulk_create(issues)
    from .narratives import generate_narratives

    generate_narratives(version)
    return version


@transaction.atomic
def delete_report_version(*, version):
    """Delete one explicitly selected version without touching live source snapshots."""
    locked_report = Report.objects.select_for_update().get(pk=version.report_id)
    locked_version = ReportVersion.objects.select_for_update().get(
        pk=version.pk, report=locked_report
    )
    active_after = timezone.now() - timedelta(seconds=settings.REPORT_ARTIFACT_STALE_SECONDS)
    if locked_version.generated_artifacts.filter(
        status=GeneratedArtifact.Status.GENERATING,
        created_at__gte=active_after,
    ).exists():
        raise ReportVersionDeleteBlocked(
            "Нельзя удалить версию, пока для неё формируется файл. Повторите после завершения."
        )

    artifact_files = [
        (artifact.file.storage, artifact.file.name)
        for artifact in locked_version.generated_artifacts.exclude(file="")
        if artifact.file.name
    ]
    # Normal snapshot deletion stays forbidden. This explicit workflow removes the
    # protected child first and then lets the version cascade delete its own rows.
    ReportDatasetSnapshot.objects.filter(version=locked_version).delete()
    locked_version.delete()
    for storage, name in artifact_files:
        transaction.on_commit(partial(_delete_artifact_file, storage, name))
    return locked_version.number


def get_report_version_data(version):
    """Read only the frozen row: deliberately has no source adapter calls."""
    return ReportDatasetSnapshot.objects.only("payload").get(version=version).payload

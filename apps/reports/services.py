import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Max

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
    normalize_count_per_day,
    shift_month,
    top_11_20_rows,
)
from .models import Report, ReportDatasetSnapshot, ReportVersion, ValidationIssue

SNAPSHOT_SCHEMA_VERSION = "mvp1.1"

DAILY_NORMALIZED_CODES = {
    "visits",
    "users",
    "new_users",
    "search_clicks",
    "search_impressions",
}


def build_position_facts(*, project, report_month, selected_dates=None):
    """Build facts independently for every search-engine/region pair; device is absent by design."""
    periods = calculate_periods(report_month)
    snapshot_filter = {"project": project}
    if selected_dates:
        snapshot_filter["snapshot_date__in"] = selected_dates
    else:
        snapshot_filter["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**snapshot_filter)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    grouped = defaultdict(dict)
    for snapshot in snapshots:
        month = snapshot.snapshot_date if selected_dates else snapshot.snapshot_date.replace(day=1)
        # The latest snapshot inside a calendar month wins deterministically.
        existing = grouped[(snapshot.search_engine, snapshot.region)].get(month)
        snapshot_key = (snapshot.snapshot_date, snapshot.created_at, str(snapshot.id))
        existing_key = (
            (existing.snapshot_date, existing.created_at, str(existing.id)) if existing else None
        )
        if existing_key is None or snapshot_key > existing_key:
            grouped[(snapshot.search_engine, snapshot.region)][month] = snapshot
    months = (
        tuple(selected_dates)
        if selected_dates
        else tuple(shift_month(periods.report.start, offset) for offset in (-2, -1, 0))
    )
    facts = []
    for (engine, region), values in sorted(grouped.items()):
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
        facts.append(
            {
                "search_engine": engine,
                "region": region,
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


def build_source_facts(*, project, report_month, selected_snapshot_ids=None):
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
        points = [
            (snapshot, {point.metric_code: point for point in snapshot.metrics.all()})
            for snapshot in snapshots
        ]
        first = points[0][1] if points else {}
        current = points[-1][1] if points else {}

        def normalized(point, snapshot):
            if point is None:
                return None
            value = point.numeric_value
            if point.metric_code in DAILY_NORMALIZED_CODES or point.metric_code.startswith(
                "source_"
            ):

                class SelectedPeriod:
                    start = snapshot.period_start
                    end = snapshot.period_end
                    days = (end - start).days + 1

                value = normalize_count_per_day(value, SelectedPeriod)
            return value

        codes = sorted({code for _snapshot, metrics in points for code in metrics})
        series = {
            code: [
                {
                    "month": snapshot.period_start,
                    "value": normalized(metrics.get(code), snapshot),
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
                normalized(new, points[-1][0]) if points else None,
                normalized(old, points[0][0]) if points else None,
                kind=kind,
            )
        extra = {}
        if source == SourceSnapshot.Source.METRIKA:
            sources = {
                code.removeprefix("source_").removesuffix("_visits"): point.numeric_value
                for code, point in current.items()
                if code.startswith("source_")
            }
            extra["traffic_sources"] = calculate_source_shares(
                current.get("visits").numeric_value if current.get("visits") else None, sources
            )
            extra["traffic_source_series"] = {
                code.removeprefix("source_").removesuffix("_visits"): series[code]
                for code in codes
                if code.startswith("source_")
            }
            extra["traffic_source_dynamics"] = {}
            for name, values in extra["traffic_source_series"].items():
                raw = sources.get(name)
                total = current.get("visits").numeric_value if current.get("visits") else None
                extra["traffic_source_dynamics"][name] = {
                    "series": values,
                    "share_percent": raw * Decimal("100") / total
                    if raw is not None and total not in (None, 0)
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
    if selected_dates:
        filters["snapshot_date__in"] = selected_dates
    else:
        filters["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**filters)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    result = []
    for item in snapshots:
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
            "payload": row.payload,
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
            "provenance": row.provenance,
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
    selected_dates = tuple(
        date.fromisoformat(value) for value in selection.get("topvisor_dates", ())
    )
    selected_source_ids = tuple(selection.get("yandex_metrika", ())) + tuple(
        selection.get("yandex_webmaster", ())
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
        "source_selection": {
            "topvisor": {
                "selected_dates": selected_dates,
                "comparison_start": selected_dates[0] if selected_dates else None,
                "comparison_end": selected_dates[-1] if selected_dates else None,
                "intermediate_dates": selected_dates[1:-1],
            },
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


def get_report_version_data(version):
    """Read only the frozen row: deliberately has no source adapter calls."""
    return ReportDatasetSnapshot.objects.only("payload").get(version=version).payload

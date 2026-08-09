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


def build_position_facts(*, project, report_month):
    """Build facts independently for every search-engine/region pair; device is absent by design."""
    periods = calculate_periods(report_month)
    snapshots = (
        RankingSnapshot.objects.filter(
            project=project, snapshot_date__range=(periods.three_months.start, periods.report.end)
        )
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    grouped = defaultdict(dict)
    for snapshot in snapshots:
        month = snapshot.snapshot_date.replace(day=1)
        # The latest snapshot inside a calendar month wins deterministically.
        existing = grouped[(snapshot.search_engine, snapshot.region)].get(month)
        snapshot_key = (snapshot.snapshot_date, snapshot.created_at, str(snapshot.id))
        existing_key = (
            (existing.snapshot_date, existing.created_at, str(existing.id)) if existing else None
        )
        if existing_key is None or snapshot_key > existing_key:
            grouped[(snapshot.search_engine, snapshot.region)][month] = snapshot
    months = tuple(shift_month(periods.report.start, offset) for offset in (-2, -1, 0))
    facts = []
    for (engine, region), values in sorted(grouped.items()):
        report_snapshot = values.get(periods.report.start)
        previous_snapshot = values.get(periods.previous.start)
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


def build_source_facts(*, project, report_month):
    periods = calculate_periods(report_month)
    snapshots = SourceSnapshot.objects.filter(
        project=project,
        period_start__range=(periods.three_months.start, periods.report.start),
    ).prefetch_related("metrics")
    indexed = {(item.source, item.period_start): item for item in snapshots}
    months = tuple(shift_month(periods.report.start, offset) for offset in (-2, -1, 0))
    result = {}
    for source in (SourceSnapshot.Source.METRIKA, SourceSnapshot.Source.WEBMASTER):
        monthly_by_start = {}
        for month in months:
            snapshot = indexed.get((source, month))
            monthly_by_start[month] = (
                {point.metric_code: point for point in snapshot.metrics.all()} if snapshot else {}
            )
        monthly = {
            "previous": monthly_by_start[periods.previous.start],
            "report": monthly_by_start[periods.report.start],
        }
        series_codes = sorted(
            {code for month_metrics in monthly_by_start.values() for code in month_metrics}
        )
        three_month_series = {}
        for code in series_codes:
            values = []
            for month in months:
                point = monthly_by_start[month].get(code)
                value = point.numeric_value if point else None
                if code in DAILY_NORMALIZED_CODES or code.startswith("source_"):
                    period = calculate_periods(month).report
                    value = normalize_count_per_day(value, period)
                values.append({"month": month, "value": value})
            three_month_series[code] = tuple(values)
        codes = sorted(set(monthly["previous"]) | set(monthly["report"]))
        changes = {}
        for code in codes:
            old, new = monthly["previous"].get(code), monthly["report"].get(code)
            old_value = old.numeric_value if old else None
            new_value = new.numeric_value if new else None
            unit = (new or old).unit
            kind = (
                ChangeKind.PERCENTAGE_POINTS
                if unit == MetricPoint.Unit.PERCENT
                else ChangeKind.VALUE
            )
            if code in DAILY_NORMALIZED_CODES or code.startswith("source_"):
                old_value = normalize_count_per_day(old_value, periods.previous)
                new_value = normalize_count_per_day(new_value, periods.report)
            changes[code] = calculate_change(new_value, old_value, kind=kind)
        extra = {}
        current = monthly["report"]
        if source == SourceSnapshot.Source.METRIKA:
            sources = {
                code.removeprefix("source_").removesuffix("_visits"): point.numeric_value
                for code, point in current.items()
                if code.startswith("source_")
            }
            extra["traffic_sources"] = calculate_source_shares(
                current.get("visits").numeric_value if current.get("visits") else None, sources
            )
            source_codes = sorted(
                code
                for month_metrics in monthly_by_start.values()
                for code in month_metrics
                if code.startswith("source_")
            )
            extra["traffic_source_series"] = {
                code.removeprefix("source_").removesuffix("_visits"): [
                    {
                        "month": month,
                        "value": (
                            normalize_count_per_day(
                                monthly_by_start[month][code].numeric_value,
                                calculate_periods(month).report,
                            )
                            if code in monthly_by_start[month]
                            else None
                        ),
                    }
                    for month in months
                ]
                for code in source_codes
            }
            extra["traffic_source_dynamics"] = {}
            for name, series in extra["traffic_source_series"].items():
                previous_value = series[-2]["value"] if len(series) > 1 else None
                current_value = series[-1]["value"] if series else None
                current_raw = sources.get(name)
                total_raw = current.get("visits").numeric_value if current.get("visits") else None
                share = (
                    current_raw * Decimal("100") / total_raw
                    if current_raw is not None and total_raw not in (None, 0)
                    else None
                )
                extra["traffic_source_dynamics"][name] = {
                    "series": series,
                    "share_percent": share,
                    "change": calculate_change(
                        current_value, previous_value, kind=ChangeKind.VALUE
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
        result[source] = {
            "normalized_changes": changes,
            "three_month_series": three_month_series,
            **extra,
        }
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


def _ranking_source_data(project, periods):
    snapshots = (
        RankingSnapshot.objects.filter(
            project=project, snapshot_date__range=(periods.three_months.start, periods.report.end)
        )
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


def _external_source_data(project, periods):
    rows = (
        SourceSnapshot.objects.filter(
            project=project,
            period_start__range=(periods.three_months.start, periods.report.start),
        )
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
            "provenance": {
                "method": row.retrieval_method,
                "checksum": row.checksum,
                "generated_at": row.generated_at,
                "generated_by_id": row.generated_by_id,
            },
        }
        for row in rows
    ]


def build_report_snapshot(*, report):
    project = report.project
    periods = calculate_periods(report.report_month)
    works = (
        WorkLogItem.objects.filter(
            project=project, work_date__range=(periods.report.start, periods.report.end)
        )
        .select_related("category")
        .order_by("work_date", "category__sort_order", "title", "id")
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
        "ranking_sources": _ranking_source_data(project, periods),
        "source_snapshots": _external_source_data(project, periods),
        "calculated": {
            "positions": build_position_facts(project=project, report_month=report.report_month),
            "sources": build_source_facts(project=project, report_month=report.report_month),
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
def create_report_version(*, report, created_by=None):
    """Explicitly freeze current source data; no version is made by reads or source updates."""
    locked_report = Report.objects.select_for_update().select_related("project").get(pk=report.pk)
    number = (locked_report.versions.aggregate(value=Max("number"))["value"] or 0) + 1
    payload = build_report_snapshot(report=locked_report)
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

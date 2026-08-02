from collections import defaultdict

from apps.metrics.models import MetricPoint, RankingSnapshot, SourceSnapshot

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
    normalize_count_per_day,
    shift_month,
)

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
    snapshots = RankingSnapshot.objects.filter(
        project=project, snapshot_date__range=(periods.three_months.start, periods.report.end)
    ).prefetch_related("positions")
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
        distribution = calculate_position_distribution(
            PositionItem(row.normalized_query, row.frequency, row.position_value)
            for row in report_rows
        )
        monthly_series = []
        for month in months:
            snapshot = values.get(month)
            if snapshot is None:
                continue
            monthly_series.append(
                {
                    "month": month,
                    "distribution": calculate_position_distribution(
                        PositionItem(row.normalized_query, row.frequency, row.position_value)
                        for row in snapshot.positions.all()
                    ),
                }
            )
        facts.append(
            {
                "search_engine": engine,
                "region": region,
                "distribution": distribution,
                "three_month_series": tuple(monthly_series),
                "semantics": compare_semantics(
                    (row.normalized_query for row in previous_rows),
                    (row.normalized_query for row in report_rows),
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

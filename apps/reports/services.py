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
        project=project, snapshot_date__range=(periods.previous.start, periods.report.end)
    ).prefetch_related("positions")
    grouped = defaultdict(dict)
    for snapshot in snapshots:
        period = (
            "report"
            if periods.report.start <= snapshot.snapshot_date <= periods.report.end
            else "previous"
        )
        # The latest snapshot inside a calendar month wins deterministically.
        existing = grouped[(snapshot.search_engine, snapshot.region)].get(period)
        if existing is None or snapshot.snapshot_date > existing.snapshot_date:
            grouped[(snapshot.search_engine, snapshot.region)][period] = snapshot
    facts = []
    for (engine, region), values in sorted(grouped.items()):
        report_rows = tuple(values["report"].positions.all()) if "report" in values else ()
        previous_rows = tuple(values["previous"].positions.all()) if "previous" in values else ()
        distribution = calculate_position_distribution(
            PositionItem(row.normalized_query, row.frequency, row.position_value)
            for row in report_rows
        )
        facts.append(
            {
                "search_engine": engine,
                "region": region,
                "distribution": distribution,
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
        project=project, period_start__in=(periods.previous.start, periods.report.start)
    ).prefetch_related("metrics")
    indexed = {(item.source, item.period_start): item for item in snapshots}
    result = {}
    for source in (SourceSnapshot.Source.METRIKA, SourceSnapshot.Source.WEBMASTER):
        monthly = {}
        for label, period in (("previous", periods.previous), ("report", periods.report)):
            snapshot = indexed.get((source, period.start))
            monthly[label] = (
                {point.metric_code: point for point in snapshot.metrics.all()} if snapshot else {}
            )
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
        result[source] = {"normalized_changes": changes, **extra}
    return {"formula_version": FORMULA_VERSION, "periods": periods, "sources": result}

from datetime import date
from decimal import Decimal

import pytest

from apps.imports.models import ImportBatch
from apps.metrics.models import KeywordPosition, MetricPoint, RankingSnapshot, SourceSnapshot
from apps.metrics.synthetic import sync_synthetic_metrics
from apps.projects.models import Project, ProjectUrlGroup, ProjectUrlRule
from apps.projects.services import classify_urls
from apps.reports.calculations import (
    ChangeKind,
    PositionItem,
    calculate_change,
    calculate_periods,
    calculate_position_distribution,
    calculate_source_shares,
    check_ctr,
    compare_semantics,
    depth_comment,
    top_11_20_rows,
)
from apps.reports.services import build_position_facts, build_source_facts


def test_calendar_periods_cross_year_and_include_three_months():
    periods = calculate_periods(date(2026, 1, 17))
    assert (periods.report.start, periods.report.end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert (periods.previous.start, periods.previous.end) == (
        date(2025, 12, 1),
        date(2025, 12, 31),
    )
    assert (periods.three_months.start, periods.three_months.end) == (
        date(2025, 11, 1),
        date(2026, 1, 31),
    )


def test_changes_handle_percent_points_zero_base_and_missing_data():
    regular = calculate_change(120, 100)
    assert regular.absolute == Decimal("20")
    assert regular.relative_percent == Decimal("20.00")

    percentage = calculate_change("15.5", "12.0", kind=ChangeKind.PERCENTAGE_POINTS)
    assert percentage.percentage_points == Decimal("3.5")
    assert percentage.relative_percent == Decimal("29.17")

    zero_base = calculate_change(5, 0)
    assert zero_base.absolute == 5
    assert zero_base.relative_percent is None
    assert zero_base.relative_unavailable_reason == "zero_base"
    assert calculate_change(0, 0).relative_percent == 0
    assert calculate_change(None, 4).relative_unavailable_reason == "missing_data"


def test_position_ranges_are_exclusive_and_top_values_are_cumulative():
    positions = [1, 3, 4, 10, 11, 20, 21, 30, 31, 50, 51, 100, 101, None]
    result = calculate_position_distribution(
        PositionItem(f"query-{index}", 1, position) for index, position in enumerate(positions)
    )
    assert result.ranges == {
        "1-3": 2,
        "4-10": 2,
        "11-20": 2,
        "21-30": 2,
        "31-50": 2,
        "51-100": 2,
    }
    assert result.top_10 == 4
    assert result.top_30 == 8
    assert result.total == 14


@pytest.mark.parametrize(
    ("depth", "expected_ranges"),
    [
        (10, {"1-3", "4-10"}),
        (20, {"1-3", "4-10", "11-20"}),
        (30, {"1-3", "4-10", "11-20", "21-30"}),
        (50, {"1-3", "4-10", "11-20", "21-30", "31-50"}),
        (100, {"1-3", "4-10", "11-20", "21-30", "31-50", "51-100"}),
    ],
)
def test_position_ranges_never_exceed_confirmed_depth(depth, expected_ranges):
    result = calculate_position_distribution([PositionItem("query", 10, None)], ranking_depth=depth)
    assert set(result.ranges) == expected_ranges
    assert depth_comment(depth).startswith(f"Проверка позиций в Google выполнена до ТОП-{depth}.")


def test_frequency_is_mandatory_for_position_calculation():
    with pytest.raises(ValueError, match="frequency"):
        PositionItem("query", None, 1)


def test_semantics_comparison_warns_on_material_query_set_change():
    result = compare_semantics(["Alpha", "Beta", "Gamma"], ["alpha", "beta", "Delta"])
    assert result.added == ("delta",)
    assert result.removed == ("gamma",)
    assert result.change_percent == Decimal("50.00")
    assert result.warning is True


def test_source_shares_zero_base_and_arithmetic_warning():
    regular = calculate_source_shares(100, {"search": 70, "direct": 30})
    assert regular.shares == {"search": Decimal("70.00"), "direct": Decimal("30.00")}
    assert regular.warning is None
    assert calculate_source_shares(0, {"search": 0}).shares["search"] == 0
    invalid = calculate_source_shares(100, {"search": 90})
    assert invalid.warning == "source_total_mismatch"


def test_ctr_arithmetic_check():
    assert check_ctr(25, 1000, Decimal("2.50")).valid is True
    mismatch = check_ctr(25, 1000, Decimal("2.70"))
    assert mismatch.valid is False
    assert mismatch.warning == "ctr_arithmetic_mismatch"
    assert check_ctr(0, 0, 0).warning == "zero_impressions"


@pytest.mark.django_db
def test_source_service_uses_monthly_totals_and_builds_source_facts():
    project = Project.objects.create(name="Demo", domain="example.com")
    sync_synthetic_metrics(project=project, report_month=date(2026, 7, 1))
    facts = build_source_facts(project=project, report_month=date(2026, 7, 1))
    metrika = facts["sources"][SourceSnapshot.Source.METRIKA]
    webmaster = facts["sources"][SourceSnapshot.Source.WEBMASTER]
    assert facts["formula_version"] == "mvp1.4-monthly-totals"
    assert metrika["normalized_changes"]["visits"].current is not None
    assert metrika["traffic_sources"].warning is None
    assert [item["month"] for item in metrika["three_month_series"]["visits"]] == [
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    ]
    assert all(item["value"] is not None for item in metrika["three_month_series"]["visits"])
    july_visits = (
        SourceSnapshot.objects.get(
            project=project,
            source=SourceSnapshot.Source.METRIKA,
            period_start=date(2026, 7, 1),
        )
        .metrics.get(metric_code="visits")
        .numeric_value
    )
    assert metrika["three_month_series"]["visits"][-1]["value"] == july_visits
    assert webmaster["ctr_check"].valid is True
    assert "indexed_pages" in webmaster["normalized_changes"]
    assert "quality_index" in webmaster["normalized_changes"]
    assert MetricPoint.objects.exists()


@pytest.mark.django_db
def test_position_service_separates_search_engine_and_region(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(name="Demo", domain="example.com")
    for index, (engine, region, position) in enumerate(
        (("yandex", "Москва", 2), ("google", "Россия", 12))
    ):
        batch = ImportBatch.objects.create(
            project=project,
            original_filename=f"{index}.csv",
            source_file=f"imports/{index}.csv",
            file_checksum=str(index) * 64,
            status=ImportBatch.Status.IMPORTED,
            snapshot_date=date(2026, 7, 31),
            search_engine=engine,
            region=region,
        )
        snapshot = RankingSnapshot.objects.create(
            project=project,
            import_batch=batch,
            snapshot_date=batch.snapshot_date,
            search_engine=engine,
            region=region,
            tracked_keyword_count=1,
        )
        KeywordPosition.objects.create(
            ranking_snapshot=snapshot,
            query=f"Query {index}",
            normalized_query=f"query {index}",
            frequency=100,
            position_raw=str(position),
            position_value=position,
            position_status=KeywordPosition.Status.RANKED,
        )
    facts = build_position_facts(project=project, report_month=date(2026, 7, 1))
    assert [(item["search_engine"], item["region"]) for item in facts["segments"]] == [
        ("yandex", "Москва"),
        ("google", "Россия"),
    ]
    assert facts["segments"][0]["distribution"].ranges["1-3"] == 1
    assert facts["segments"][1]["distribution"].ranges["11-20"] == 1


@pytest.mark.django_db
def test_position_service_includes_all_three_months(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(name="Demo", domain="example.com")
    for index, (month, position) in enumerate(
        ((date(2026, 5, 31), 25), (date(2026, 6, 30), 9), (date(2026, 7, 31), 2))
    ):
        batch = ImportBatch.objects.create(
            project=project,
            original_filename=f"month-{index}.csv",
            source_file=f"imports/month-{index}.csv",
            file_checksum=str(index + 3) * 64,
            status=ImportBatch.Status.IMPORTED,
            snapshot_date=month,
            search_engine="yandex",
            region="Москва",
        )
        snapshot = RankingSnapshot.objects.create(
            project=project,
            import_batch=batch,
            snapshot_date=month,
            search_engine="yandex",
            region="Москва",
            tracked_keyword_count=1,
        )
        KeywordPosition.objects.create(
            ranking_snapshot=snapshot,
            query="Query",
            normalized_query="query",
            frequency=100,
            position_raw=str(position),
            position_value=position,
            position_status=KeywordPosition.Status.RANKED,
        )

    facts = build_position_facts(project=project, report_month=date(2026, 7, 1))
    series = facts["segments"][0]["three_month_series"]
    assert [item["month"] for item in series] == [
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    ]
    assert series[0]["distribution"].ranges["21-30"] == 1
    assert series[1]["distribution"].ranges["4-10"] == 1
    assert series[2]["distribution"].ranges["1-3"] == 1

    selected = build_position_facts(
        project=project,
        report_month=date(2026, 7, 1),
        selected_dates={"yandex": (date(2026, 5, 31), date(2026, 7, 31))},
    )["segments"][0]
    assert [item["month"] for item in selected["three_month_series"]] == [
        date(2026, 5, 31),
        date(2026, 7, 31),
    ]
    assert [item["month"] for item in selected["chart_series"]] == [
        date(2026, 5, 31),
        date(2026, 7, 31),
    ]


@pytest.mark.django_db
def test_url_group_batch_facts_retain_all_intersections_and_warning():
    project = Project.objects.create(name="Demo", domain="example.com")
    broad = ProjectUrlGroup.objects.create(project=project, name="Catalog", priority=10)
    exact = ProjectUrlGroup.objects.create(project=project, name="Shoes", priority=20)
    ProjectUrlRule.objects.create(group=broad, type="contains", pattern="/catalog/")
    ProjectUrlRule.objects.create(group=exact, type="contains", pattern="/catalog/shoes/")
    url = "https://example.com/catalog/shoes/red"
    fact = classify_urls(project, [url])[url]
    assert fact.group == exact
    assert fact.overlapping_groups == (exact, broad)
    assert fact.warnings == ("url_group_overlap",)


def test_top_11_20_preserves_optional_group_and_target_url():
    row = PositionItem("seo report", 100, 12, "Reports", "https://example.com/report/")
    result = top_11_20_rows([row], depth=20, mode="enabled")
    assert result[0].group == "Reports"
    assert result[0].target_url == "https://example.com/report/"
    assert top_11_20_rows([row], depth=20, mode="auto") == result
    assert top_11_20_rows([row], depth=20, mode="disabled") == ()
    assert top_11_20_rows([row], depth=10, mode="enabled") == ()

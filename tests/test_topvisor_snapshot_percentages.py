import json
from datetime import date

import pytest

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.forms import ReportCreateForm
from apps.reports.models import ProjectReportSettings
from apps.reports.runtime_fixes_round2 import _manual_topvisor_segment
from apps.reports.topvisor_percentages import repair_stale_topvisor_percentages
from apps.reports.views import _topvisor_editor_data
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def snapshot(project, day, *, total=5111, top3=1332, top10=2761, top11=1210, configuration="main"):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=day,
        search_engine="yandex",
        region="Москва",
        topvisor_configuration_id=configuration,
        tracked_keyword_count=total,
        ranking_depth=100,
        visibility="44.31",
        provenance={"tops": {"all": total, "1_3": top3, "1_10": top10, "11_30": top11}},
    )


@pytest.fixture
def project():
    project = Project.objects.create(
        name="Changing keyword sets",
        domain="snapshot-percentages.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        selected_configurations=[
            {"id": "main", "search_engine": "yandex", "region_name": "Москва", "depth": 100}
        ],
    )
    snapshot(project, date(2026, 9, 30))
    snapshot(project, date(2026, 10, 31))
    for day in (date(2026, 11, 1), date(2026, 11, 2)):
        snapshot(project, day, total=29, top3=1, top10=8, top11=10)
    return project


def broken_row(*, capped=True):
    return {
        "configuration_id": "main",
        "engine": "yandex",
        "region": "Москва",
        "month": "2026-10-01",
        "snapshot_date": "2026-10-31",
        "include_in_report": True,
        "deleted": False,
        "manual_override": True,
        "visibility": 12.5,
        "automatic_visibility": 44,
        "total": 29,
        "top3": 1332,
        "top10": 2761,
        "top11_30": 1210,
        "top3_percent": 100 if capped else 4593,
        "top10_percent": 100 if capped else 9521,
        "top11_30_percent": 100 if capped else 4172,
    }


def test_later_small_checks_do_not_rebase_any_historical_month(project):
    rows, _segments = _topvisor_editor_data(project)
    by_month = {row["month"]: row for row in rows}
    for month in ("2026-09-01", "2026-10-01"):
        row = by_month[month]
        assert row["total"] == 5111
        assert (row["top3"], row["top10"], row["top11_30"]) == (1332, 2761, 1210)
        assert (row["top3_percent"], row["top10_percent"], row["top11_30_percent"]) == (26, 54, 24)
    assert by_month["2026-11-01"]["total"] == 29
    assert by_month["2026-11-01"]["top10_percent"] == 28


def test_counts_and_denominator_belong_to_the_same_selected_day(project):
    snapshot(project, date(2026, 10, 15), total=6000, top3=1800, top10=3000, top11=900)
    rows, _segments = _topvisor_editor_data(
        project, selected_dates_by_configuration={"main": {"dates": ["2026-10-15"]}}
    )
    october = next(row for row in rows if row["month"] == "2026-10-01")
    assert october["snapshot_date"] == "2026-10-15"
    assert october["total"] == 6000
    assert (october["top3_percent"], october["top10_percent"]) == (30, 50)


def test_missing_provider_tops_use_positions_from_that_check(project):
    august = snapshot(project, date(2026, 8, 31), total=4)
    august.provenance = {}
    august.save(update_fields=["provenance"])
    for index, position in enumerate((1, 7, 20, None)):
        KeywordPosition.objects.create(
            ranking_snapshot=august,
            query=f"query {index}",
            normalized_query=f"query {index}",
            frequency=1,
            position_value=position,
            position_status="ranked" if position else "not_found",
        )
    rows, _segments = _topvisor_editor_data(project)
    august_row = next(row for row in rows if row["month"] == "2026-08-01")
    assert august_row["total"] == 4
    assert (august_row["top3_percent"], august_row["top10_percent"]) == (25, 50)
    assert august_row["top11_30_percent"] == 25


@pytest.mark.parametrize("capped", [True, False])
@pytest.mark.parametrize("checked", [True, False])
def test_known_saved_bug_is_repaired_without_changing_user_choices(project, capped, checked):
    row = {**broken_row(capped=capped), "include_in_report": checked, "deleted": not checked}
    original = json.dumps([row])
    repaired = json.loads(repair_stale_topvisor_percentages(project, original))[0]
    assert repaired == {
        **row,
        "total": 5111,
        "top3_percent": 26,
        "top10_percent": 54,
        "top11_30_percent": 24,
    }
    assert row["total"] == 29
    assert not ProjectReportSettings.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"top3_percent": 12.34},
        {"top3": 1333},
        {"configuration_id": "other"},
        {"snapshot_date": "2026-10-01"},
        {"total": 5111, "top3_percent": 26, "top10_percent": 54, "top11_30_percent": 24},
    ],
)
def test_manual_edits_and_unmatched_or_already_valid_rows_are_preserved(project, changes):
    original = json.dumps([{**broken_row(), **changes}])
    assert repair_stale_topvisor_percentages(project, original) == original


@pytest.mark.parametrize("checked", [True, False])
def test_old_common_denominator_is_repaired_only_for_automatic_rows(project, checked):
    row = {
        **broken_row(),
        "include_in_report": checked,
        "manual_override": False,
        "visibility": None,
        "total": 6000,
        "top3_percent": 22,
        "top10_percent": 46,
        "top11_30_percent": 20,
    }
    repaired = repair_stale_topvisor_percentages(project, [row])[0]
    assert repaired["total"] == 5111
    assert repaired["top3_percent"] == 26
    assert repaired["include_in_report"] is checked
    assert repaired["manual_override"] is False
    manual = {**row, "manual_override": True}
    assert repair_stale_topvisor_percentages(project, [manual]) == [manual]


def test_saved_bug_is_repaired_on_open_and_submit_without_a_provider_refresh(project):
    original = json.dumps([broken_row()])
    settings = ProjectReportSettings.objects.create(
        project=project,
        values={"topvisor_manual_rows": original, "completed_work_text": "Keep this setting"},
    )
    initial = ReportCreateForm(project=project).initial["topvisor_manual_rows"]
    assert json.loads(initial)[0]["top10_percent"] == 54
    settings.refresh_from_db()
    assert settings.values["topvisor_manual_rows"] == original
    form = ReportCreateForm(
        {
            "month": "2026-10",
            "yandex_dates": ["2026-09-30", "2026-10-31"],
            "topvisor_manual_rows": original,
        },
        project=project,
    )
    assert form.is_valid(), form.errors
    repaired = json.loads(form.cleaned_data["topvisor_manual_rows"])
    result = _manual_topvisor_segment(
        {"display_options": {"topvisor_manual_rows": repaired}},
        {
            "search_engine": "yandex",
            "region": "Москва",
            "configuration_id": "main",
            "ranking_depth": 100,
            "three_month_series": [],
        },
    )
    buckets = result["monthly_table_series"][0]["distribution"]["manual_buckets"]
    assert buckets["1-3"] == {"count": 1332, "share": 26}
    assert buckets["1-10"] == {"count": 2761, "share": 54}
    settings.refresh_from_db()
    assert settings.values["completed_work_text"] == "Keep this setting"

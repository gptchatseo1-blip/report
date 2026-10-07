import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports import services, views
from apps.reports.forms import ReportCreateForm
from apps.reports.models import ProjectReportSettings
from apps.reports.runtime_fixes_round7 import (
    _follow_monthly_table_toggle,
    provider_visibility,
)
from apps.reports.topvisor_editor_maintenance import (
    clear_editor_segment,
    refresh_provider_visibility,
)
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def _project():
    project = Project.objects.create(
        name="Topvisor raw visibility",
        domain="topvisor-raw.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        selected_configurations=[
            {
                "id": "yandex-main",
                "search_engine": "yandex",
                "region_index": 1,
                "region_name": "Москва",
                "depth": 100,
            }
        ],
    )
    return project


def _snapshot(project, day, stored, raw):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=day,
        search_engine="yandex",
        region="Москва",
        ranking_depth=100,
        depth_source=RankingSnapshot.DepthSource.TOPVISOR_API,
        topvisor_configuration_id="yandex-main",
        visibility=stored,
        visibility_raw={
            "value": raw,
            "source": "topvisor_api_summary_chart",
        },
        provenance={
            "visibility": {
                "value": raw,
                "source": "topvisor_api_summary_chart",
            }
        },
        response_checksum=f"{day}-{raw}",
    )


class _SummaryClient:
    def __init__(self, values, tops=None):
        self.values = values
        self.tops = tops or {}
        self.calls = []

    def get_summary_chart(self, project_id, *, region_index, dates):
        self.calls.append((str(project_id), int(region_index), tuple(dates)))
        return {
            "dates": list(dates),
            "seriesByProjectsId": {
                str(project_id): {
                    "visibility": [self.values.get(day) for day in dates],
                    "tops": {
                        label: [values.get(day) for day in dates]
                        for label, values in self.tops.items()
                    },
                }
            },
        }


def test_provider_visibility_prefers_exact_raw_topvisor_value():
    project = _project()
    snapshot = _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")

    assert provider_visibility(snapshot) == Decimal("15.65")


def test_editor_uses_raw_provider_visibility_instead_of_stale_stored_integer():
    project = _project()
    _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")

    rows, _segments = views._topvisor_editor_data(project)

    assert rows[-1]["visibility"] == 16.0


def test_live_refresh_replaces_stale_provider_snapshot_before_clear():
    project = _project()
    snapshot = _snapshot(project, date(2026, 8, 31), "15.0000", "15")
    client = _SummaryClient({"2026-08-31": "15.65"})

    updated = refresh_provider_visibility(
        project,
        engine="yandex",
        region="Москва",
        client=client,
    )

    snapshot.refresh_from_db()
    assert updated == 1
    assert snapshot.visibility == Decimal("15.6500")
    assert snapshot.visibility_raw["value"] == "15.65"
    assert snapshot.provenance["visibility"]["value"] == "15.65"
    assert client.calls == [("42", 1, ("2026-08-31",))]


def test_live_refresh_stores_exact_provider_top_counts_and_editor_percentages():
    project = _project()
    snapshot = _snapshot(project, date(2026, 8, 17), "15.0000", "15")
    client = _SummaryClient(
        {"2026-08-17": "15.65"},
        tops={
            "1-3": {"2026-08-17": 253},
            "1-10": {"2026-08-17": 754},
            "11-30": {"2026-08-17": 826},
            "all": {"2026-08-17": 2974},
        },
    )

    updated = refresh_provider_visibility(project, client=client)

    snapshot.refresh_from_db()
    rows, _segments = views._topvisor_editor_data(project)
    assert updated == 1
    assert snapshot.provenance["tops"] == {
        "1_3": 253,
        "1_10": 754,
        "11_30": 826,
        "all": 2974,
    }
    assert rows[-1]["total"] == 2974
    assert rows[-1]["top3"] == 253
    assert rows[-1]["top10"] == 754
    assert rows[-1]["top11_30"] == 826
    assert rows[-1]["top3_percent"] == 9
    assert rows[-1]["top10_percent"] == 25
    assert rows[-1]["top11_30_percent"] == 28


def test_editor_uses_newest_keyword_total_for_every_month_and_whole_percentages():
    project = _project()
    august = _snapshot(project, date(2026, 8, 17), "15.0000", "15")
    september = _snapshot(project, date(2026, 9, 21), "16.0000", "16")
    august.provenance["tops"] = {
        "1_3": 253,
        "1_10": 754,
        "11_30": 826,
        "all": 2396,
    }
    september.provenance["tops"] = {
        "1_3": 290,
        "1_10": 884,
        "11_30": 1066,
        "all": 2974,
    }
    august.save(update_fields=["provenance"])
    september.save(update_fields=["provenance"])

    rows, _segments = views._topvisor_editor_data(project)
    by_month = {row["month"][:7]: row for row in rows}

    assert by_month["2026-08"]["total"] == 2974
    assert by_month["2026-08"]["top3_percent"] == 9
    assert by_month["2026-08"]["top10_percent"] == 25
    assert by_month["2026-08"]["top11_30_percent"] == 28
    assert by_month["2026-09"]["total"] == 2974
    assert by_month["2026-09"]["top3_percent"] == 10
    assert by_month["2026-09"]["top10_percent"] == 30
    assert by_month["2026-09"]["top11_30_percent"] == 36


def test_editor_rejects_isolated_partial_latest_snapshot_and_keeps_exact_source_date():
    project = _project()
    first = _snapshot(project, date(2026, 9, 1), "31.08", "31.08")
    selected = _snapshot(project, date(2026, 9, 20), "29.85", "29.85")
    partial = _snapshot(project, date(2026, 9, 24), "10.94", "10.94")
    first.tracked_keyword_count = 5084
    first.provenance["tops"] = {"all": 5084, "1_3": 1100, "1_10": 2500, "11_30": 1500}
    selected.tracked_keyword_count = 5111
    selected.provenance["tops"] = {
        "all": 5111,
        "1_3": 1042,
        "1_10": 2426,
        "11_30": 1587,
    }
    partial.tracked_keyword_count = 75
    partial.provenance["tops"] = {"all": 75, "1_3": 6, "1_10": 40, "11_30": 31}
    for snapshot in (first, selected, partial):
        snapshot.save(update_fields=["tracked_keyword_count", "provenance"])

    rows, _segments = views._topvisor_editor_data(project)

    september = next(row for row in rows if row["month"] == "2026-09-01")
    assert september["snapshot_date"] == "2026-09-20"
    assert september["total"] == 5111
    assert september["top10"] == 2426

    form = ReportCreateForm(project=project)
    available = {value for value, _label in form.fields["yandex_dates"].choices}
    assert "2026-09-20" in available
    assert "2026-09-24" not in available


def test_editor_uses_exact_selected_complete_day_inside_month():
    project = _project()
    early = _snapshot(project, date(2026, 8, 10), "12", "12")
    late = _snapshot(project, date(2026, 8, 25), "15", "15")
    early.tracked_keyword_count = 1000
    late.tracked_keyword_count = 1000
    early.provenance["tops"] = {"all": 1000, "1_3": 100, "1_10": 300, "11_30": 250}
    late.provenance["tops"] = {"all": 1000, "1_3": 200, "1_10": 400, "11_30": 300}
    early.save(update_fields=["tracked_keyword_count", "provenance"])
    late.save(update_fields=["tracked_keyword_count", "provenance"])

    rows, _segments = views._topvisor_editor_data(
        project,
        selected_dates_by_engine={"yandex": ["2026-08-10"]},
    )

    august = next(row for row in rows if row["month"] == "2026-08-01")
    assert august["snapshot_date"] == "2026-08-10"
    assert august["top3"] == 100


def test_clear_after_live_refresh_returns_current_topvisor_display_value():
    project = _project()
    _snapshot(project, date(2026, 8, 31), "15.0000", "15")
    ProjectReportSettings.objects.create(
        project=project,
        values={
            "topvisor_manual_rows": json.dumps(
                [
                    {
                        "configuration_id": "yandex-main",
                        "engine": "yandex",
                        "region": "Москва",
                        "month": "2026-08-01",
                        "include_in_report": True,
                        "deleted": False,
                        "manual_override": True,
                        "visibility": 15,
                        "automatic_visibility": 15,
                        "total": 0,
                        "top3": 0,
                        "top10": 0,
                        "top11_30": 0,
                        "top3_percent": 0,
                        "top10_percent": 0,
                        "top11_30_percent": 0,
                    }
                ]
            )
        },
    )
    client = _SummaryClient({"2026-08-31": "15.65"})

    refresh_provider_visibility(
        project,
        engine="yandex",
        region="Москва",
        client=client,
    )
    cleared = clear_editor_segment(project, "yandex", "Москва")

    assert cleared[0]["visibility"] is None
    assert cleared[0]["automatic_visibility"] == 16.0
    assert cleared[0]["manual_override"] is False


def test_clear_replaces_manual_visibility_with_recovered_automatic_value():
    project = _project()
    _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")
    ProjectReportSettings.objects.create(
        project=project,
        values={
            "topvisor_manual_rows": json.dumps(
                [
                    {
                        "configuration_id": "yandex-main",
                        "engine": "yandex",
                        "region": "Москва",
                        "month": "2026-08-01",
                        "include_in_report": True,
                        "deleted": False,
                        "manual_override": True,
                        "visibility": 15,
                        "automatic_visibility": 15,
                        "total": 0,
                        "top3": 0,
                        "top10": 0,
                        "top11_30": 0,
                        "top3_percent": 0,
                        "top10_percent": 0,
                        "top11_30_percent": 0,
                    }
                ]
            )
        },
    )

    cleared = clear_editor_segment(project, "yandex", "Москва")

    assert cleared[0]["visibility"] is None
    assert cleared[0]["automatic_visibility"] == 16.0
    assert cleared[0]["manual_override"] is False


def test_report_facts_use_exact_raw_visibility_and_provider_display_rounding():
    project = _project()
    _snapshot(project, date(2026, 7, 31), "14.0000", "14.40")
    _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")

    facts = services.build_position_facts(
        project=project,
        report_month=date(2026, 8, 1),
        selected_dates={
            "yandex": (date(2026, 7, 31), date(2026, 8, 31)),
        },
    )
    segment = facts["segments"][0]

    assert segment["three_month_series"][-1]["visibility"] == Decimal("15.65")
    assert segment["visibility_change"].current == Decimal("16")
    assert segment["visibility_change"].previous == Decimal("14")


def test_monthly_table_toggle_follows_single_dynamics_checkbox():
    enabled = _follow_monthly_table_toggle(
        {
            "include_monthly_dynamics": True,
            "include_monthly_dynamics_table": False,
        }
    )
    disabled = _follow_monthly_table_toggle(
        {
            "include_monthly_dynamics": False,
            "include_monthly_dynamics_table": True,
        }
    )

    assert enabled["include_monthly_dynamics_table"] is True
    assert disabled["include_monthly_dynamics_table"] is False


def test_round7_ui_removes_duplicate_toggle_and_uses_edit_label_and_equal_height():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/reports/report-polish-round7.js").read_text()
    css = (root / "static/reports/report-polish-round7.css").read_text()

    assert "trigger.textContent = 'Редактировать'" in js
    assert "tableLabel?.remove()" in js
    assert "tableToggle.checked = dynamics.checked" in js
    assert "Редактировать таблицы динамики" in js
    assert "height:42px" in css
    assert "data-topvisor-clear-segment" in css
    assert "manual-add-row" in css
    assert "data-topvisor-refresh-editor" in css
    assert "data-topvisor-segment-maintenance" in css
    assert "margin:0!important" in css
    assert "align-items:center!important" in css

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports import services, views
from apps.reports.models import ProjectReportSettings
from apps.reports.runtime_fixes_round7 import (
    _follow_monthly_table_toggle,
    provider_visibility,
)
from apps.reports.topvisor_editor_maintenance import clear_editor_segment
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


def test_provider_visibility_prefers_exact_raw_topvisor_value():
    project = _project()
    snapshot = _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")

    assert provider_visibility(snapshot) == Decimal("15.65")


def test_editor_uses_raw_provider_visibility_instead_of_stale_stored_integer():
    project = _project()
    _snapshot(project, date(2026, 8, 31), "15.0000", "15.65")

    rows, _segments = views._topvisor_editor_data(project)

    assert rows[-1]["visibility"] == 16.0


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

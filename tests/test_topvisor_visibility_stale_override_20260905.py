import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports import views
from apps.reports.forms import ReportCreateForm
from apps.reports.models import ProjectReportSettings, ReportDatasetSnapshot
from apps.reports.runtime_fixes_round5 import sanitize_stale_topvisor_visibility
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def _project_with_visibility_history():
    project = Project.objects.create(
        name="Topvisor stale visibility",
        domain="stale-visibility.example",
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
    for day, visibility in ((date(2026, 7, 31), "14.90"), (date(2026, 8, 31), "15.65")):
        RankingSnapshot.objects.create(
            project=project,
            snapshot_date=day,
            search_engine="yandex",
            region="Москва",
            topvisor_configuration_id="yandex-main",
            ranking_depth=100,
            visibility=visibility,
            response_checksum=f"{day}-{visibility}",
        )
    return project


def _stale_row(*, explicit=True):
    row = {
        "configuration_id": "yandex-main",
        "engine": "yandex",
        "region": "Москва",
        "month": "2026-08-01",
        "visibility": 15,
        "automatic_visibility": 16,
        "include_in_report": True,
        "deleted": False,
        "total": 0,
        "top3": 0,
        "top10": 0,
        "top11_30": 0,
        "top3_percent": 0,
        "top10_percent": 0,
        "top11_30_percent": 0,
    }
    if explicit:
        row["manual_override"] = True
    else:
        row.pop("automatic_visibility")
        row.pop("include_in_report")
        row.pop("deleted")
    return row


def _sanitize(project, row):
    payload = sanitize_stale_topvisor_visibility(project, json.dumps([row]))
    return json.loads(payload)[0]


def test_stale_floor_rounded_visibility_is_removed_even_after_round2_saved_it():
    project = _project_with_visibility_history()
    cleaned = _sanitize(project, _stale_row())

    assert cleaned["visibility"] is None
    assert cleaned["automatic_visibility"] == 16.0
    assert cleaned["manual_override"] is False


def test_legacy_stale_visibility_is_removed_but_real_manual_value_is_kept():
    project = _project_with_visibility_history()
    stale, manual = _stale_row(explicit=False), _stale_row(explicit=True)
    manual["visibility"] = 13.5

    cleaned_stale = _sanitize(project, stale)
    cleaned_manual = _sanitize(project, manual)

    assert cleaned_stale["visibility"] is None
    assert cleaned_manual["visibility"] == 13.5
    assert cleaned_manual["manual_override"] is True


def test_report_page_initial_state_no_longer_overlays_15_on_automatic_16():
    project = _project_with_visibility_history()
    ProjectReportSettings.objects.create(
        project=project,
        values={"topvisor_manual_rows": json.dumps([_stale_row()])},
    )

    form = ReportCreateForm(project=project)
    saved = json.loads(form.initial["topvisor_manual_rows"])[0]
    editor_rows, _segments = views._topvisor_editor_data(project)
    august = next(row for row in editor_rows if row["month"].startswith("2026-08"))

    assert saved["visibility"] is None
    assert saved["manual_override"] is False
    assert august["visibility"] == 16.0


def test_report_creation_with_previous_stale_15_produces_current_visibility_16(client):
    project = _project_with_visibility_history()
    user = get_user_model().objects.create_user(
        "visibility-real-flow",
        password="test-password",
    )
    client.force_login(user)

    response = client.post(
        reverse("reports:report-create", args=[project.id]),
        {
            "yandex_dates": ["2026-07-31", "2026-08-31"],
            "topvisor_manual_rows": json.dumps([_stale_row()]),
        },
    )

    assert response.status_code == 302
    snapshot = ReportDatasetSnapshot.objects.get()
    segment = snapshot.payload["calculated"]["positions"]["segments"][0]
    assert segment["visibility_change"]["previous"] == 15
    assert segment["visibility_change"]["current"] == 16
    assert segment["visibility_change"]["percentage_points"] == 1
    frozen_row = snapshot.payload["display_options"]["topvisor_manual_rows"][0]
    assert frozen_row["visibility"] is None
    assert frozen_row["manual_override"] is False

    persisted = ProjectReportSettings.objects.get(project=project).values["topvisor_manual_rows"]
    assert json.loads(persisted)[0]["visibility"] is None

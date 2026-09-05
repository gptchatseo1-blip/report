import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports.models import ProjectReportSettings
from apps.reports.topvisor_editor_maintenance import clear_editor_segment, refresh_editor_rows
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def _project_with_snapshots():
    project = Project.objects.create(
        name="Topvisor editor maintenance",
        domain="editor-maintenance.example",
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


def _row(month, *, visibility, automatic_visibility, manual_override, include=True, deleted=False):
    return {
        "configuration_id": "yandex-main",
        "engine": "yandex",
        "region": "Москва",
        "month": month,
        "include_in_report": include,
        "deleted": deleted,
        "manual_override": manual_override,
        "visibility": visibility,
        "automatic_visibility": automatic_visibility,
        "total": 0,
        "top3": 0,
        "top10": 0,
        "top11_30": 0,
        "top3_percent": 0,
        "top10_percent": 0,
        "top11_30_percent": 0,
    }


def _save(project, rows):
    ProjectReportSettings.objects.create(
        project=project,
        values={"topvisor_manual_rows": json.dumps(rows)},
    )


def test_refresh_reloads_automatic_values_but_keeps_real_manual_corrections():
    project = _project_with_snapshots()
    july_manual = _row(
        "2026-07-01",
        visibility=12.5,
        automatic_visibility=15,
        manual_override=True,
    )
    august_stale = _row(
        "2026-08-01",
        visibility=15,
        automatic_visibility=16,
        manual_override=True,
    )
    september_manual = _row(
        "2026-09-01",
        visibility=9,
        automatic_visibility=None,
        manual_override=True,
    )
    _save(project, [july_manual, august_stale, september_manual])

    refreshed = refresh_editor_rows(project)
    by_month = {row["month"][:7]: row for row in refreshed}

    assert by_month["2026-07"]["visibility"] == 12.5
    assert by_month["2026-07"]["manual_override"] is True
    assert by_month["2026-07"]["automatic_visibility"] == 15.0
    assert by_month["2026-08"]["visibility"] is None
    assert by_month["2026-08"]["manual_override"] is False
    assert by_month["2026-08"]["automatic_visibility"] == 16.0
    assert by_month["2026-09"]["visibility"] == 9


def test_clear_segment_removes_manual_rows_and_restores_automatic_values():
    project = _project_with_snapshots()
    july_manual = _row(
        "2026-07-01",
        visibility=12.5,
        automatic_visibility=15,
        manual_override=True,
        include=True,
        deleted=True,
    )
    september_manual = _row(
        "2026-09-01",
        visibility=9,
        automatic_visibility=None,
        manual_override=True,
    )
    other_segment = {
        **_row(
            "2026-08-01",
            visibility=7,
            automatic_visibility=8,
            manual_override=True,
        ),
        "engine": "google",
    }
    _save(project, [july_manual, september_manual, other_segment])

    cleared = clear_editor_segment(project, "yandex", "Москва")

    yandex_rows = [row for row in cleared if row["engine"] == "yandex"]
    assert len(yandex_rows) == 1
    assert yandex_rows[0]["month"] == "2026-07-01"
    assert yandex_rows[0]["visibility"] is None
    assert yandex_rows[0]["automatic_visibility"] == 15.0
    assert yandex_rows[0]["manual_override"] is False
    assert yandex_rows[0]["include_in_report"] is True
    assert yandex_rows[0]["deleted"] is False

    google_rows = [row for row in cleared if row["engine"] == "google"]
    assert google_rows[0]["visibility"] == 7
    assert google_rows[0]["manual_override"] is True


def test_refresh_and_clear_endpoints_persist_changes(client):
    project = _project_with_snapshots()
    _save(
        project,
        [
            _row(
                "2026-08-01",
                visibility=15,
                automatic_visibility=16,
                manual_override=True,
            )
        ],
    )
    user = get_user_model().objects.create_user(
        "topvisor-editor-maintenance",
        password="test-password",
    )
    client.force_login(user)

    refresh = client.post(reverse("reports:topvisor-editor-refresh", args=[project.id]))
    assert refresh.status_code == 200
    assert refresh.json()["ok"] is True
    settings = ProjectReportSettings.objects.get(project=project)
    refreshed = json.loads(settings.values["topvisor_manual_rows"])
    assert refreshed[0]["visibility"] is None
    assert refreshed[0]["automatic_visibility"] == 16.0

    clear = client.post(
        reverse("reports:topvisor-editor-clear", args=[project.id]),
        data=json.dumps({"engine": "yandex", "region": "Москва"}),
        content_type="application/json",
    )
    assert clear.status_code == 200
    assert clear.json()["ok"] is True
    settings.refresh_from_db()
    cleared = json.loads(settings.values["topvisor_manual_rows"])
    assert cleared[0]["manual_override"] is False
    assert cleared[0]["visibility"] is None

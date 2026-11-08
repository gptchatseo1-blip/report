import json
import urllib.request
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports import services
from apps.reports.forms import ReportCreateForm
from apps.reports.models import ProjectReportSettings
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def test_live_https_cannot_open_a_transport_connection():
    with patch("socket.create_connection") as transport:
        with pytest.raises(AssertionError, match="Live HTTP connections are disabled"):
            urllib.request.urlopen("https://example.invalid/")
        transport.assert_not_called()


def _snapshot(project, day, visibility, *, engine="yandex", region="Москва", configuration="ya"):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=day,
        search_engine=engine,
        region=region,
        topvisor_configuration_id=configuration,
        tracked_keyword_count=100,
        visibility=visibility,
        visibility_raw={"value": visibility, "source": "topvisor_api_summary_chart"},
        provenance={"tops": {"all": 100, "1_3": 10, "1_10": 40, "11_30": 30}},
    )


@pytest.fixture
def project(client):
    client.force_login(get_user_model().objects.create_user("editor-calendar"))
    project = Project.objects.create(
        name="Exact visibility dates",
        domain="editor-calendar.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        selected_configurations=[
            {"id": "ya", "search_engine": "yandex", "region_name": "Москва", "depth": 100},
            {"id": "go", "search_engine": "google", "region_name": "Москва", "depth": 100},
        ],
    )
    for day, value in (
        (date(2026, 9, 20), "28"),
        (date(2026, 10, 17), "38"),
        (date(2026, 10, 27), "44"),
    ):
        _snapshot(project, day, value)
    for day, value in ((date(2026, 9, 20), "50"), (date(2026, 10, 17), "60")):
        _snapshot(project, day, value, engine="google", configuration="go")
    return project


def _preview(client, project, selected):
    return client.post(
        reverse("reports:topvisor-editor-preview", args=[project.id]),
        json.dumps({"selected_dates_by_field": selected}),
        content_type="application/json",
    )


def test_preview_reads_selected_day_without_changing_settings_snapshots_or_graph(project, client):
    saved = {
        "completed_work_text": "Keep this",
        "topvisor_manual_rows": json.dumps(
            [
                {
                    "engine": "yandex",
                    "month": "2026-10-01",
                    "visibility": 44,
                    "include_in_report": True,
                }
            ]
        ),
    }
    settings = ProjectReportSettings.objects.create(project=project, values=saved)
    dates = {"yandex": [date(2026, 9, 20), date(2026, 10, 17)]}
    before = services.build_position_facts(
        project=project, report_month=date(2026, 10, 1), selected_dates=dates
    )
    with patch("urllib.request.urlopen", side_effect=AssertionError("No provider requests")):
        response = _preview(
            client,
            project,
            {"yandex_dates": ["2026-09-20", "2026-10-17"], "google_dates": ["2026-10-17"]},
        )
    assert response.status_code == 200
    rows = response.json()["rows"]
    yandex = [row for row in rows if row["engine"] == "yandex"]
    assert [(row["snapshot_date"], row["visibility"]) for row in yandex] == [
        ("2026-09-20", 28),
        ("2026-10-17", 38),
    ]
    assert next(row for row in rows if row["engine"] == "google")["visibility"] == 60
    settings.refresh_from_db()
    assert settings.values == saved
    assert (
        RankingSnapshot.objects.get(project=project, snapshot_date=date(2026, 10, 27)).visibility
        == 44
    )
    after = services.build_position_facts(
        project=project, report_month=date(2026, 10, 1), selected_dates=dates
    )
    assert after == before
    assert [point["visibility"] for point in after["segments"][0]["chart_series"]] == [
        Decimal("28"),
        Decimal("38"),
    ]


def test_preview_tracks_a_changed_calendar_and_uses_latest_selected_day_inside_month(
    project, client
):
    first = _preview(client, project, {"yandex_dates": ["2026-10-17"]}).json()["rows"]
    second = _preview(client, project, {"yandex_dates": ["2026-10-17", "2026-10-27"]}).json()[
        "rows"
    ]
    assert first[0]["visibility"] == 38
    assert second[0]["visibility"] == 44
    assert second[0]["snapshot_date"] == "2026-10-27"
    assert not ProjectReportSettings.objects.exists()


def test_preview_keeps_three_configuration_calendars_separate(project, client):
    mapping = project.topvisor_mapping
    mapping.selected_configurations.append(
        {"id": "ya-spb", "search_engine": "yandex", "region_name": "Санкт-Петербург", "depth": 100}
    )
    mapping.save(update_fields=["selected_configurations"])
    _snapshot(project, date(2026, 10, 17), "52", region="Санкт-Петербург", configuration="ya-spb")
    form = ReportCreateForm(project=project)
    assert form.use_configuration_calendars
    selected = {item["field_name"]: ["2026-10-17"] for item in form.configuration_date_fields}
    rows = _preview(client, project, selected).json()["rows"]
    assert {row["configuration_id"]: row["visibility"] for row in rows} == {
        "ya": 38,
        "ya-spb": 52,
        "go": 60,
    }


@pytest.mark.parametrize(
    "selected", [{"yandex_dates": "2026-10-17"}, {"yandex_dates": ["2026-10-18"]}, []]
)
def test_preview_rejects_invalid_dates(project, client, selected):
    assert _preview(client, project, selected).status_code == 400


def test_preview_requires_login_and_post(project, client):
    url = reverse("reports:topvisor-editor-preview", args=[project.id])
    assert client.get(url).status_code == 405
    client.logout()
    assert _preview(client, project, {"yandex_dates": ["2026-10-17"]}).status_code == 302

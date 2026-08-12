from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import MetricPoint, RankingSnapshot, SourceSnapshot
from apps.projects.models import Project
from apps.reports.forms import ReportCreateForm
from apps.reports.models import Report
from apps.reports.services import build_source_facts
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def ranking(project, day, configuration):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=day,
        search_engine=configuration,
        region="Москва",
        ranking_depth=20,
        topvisor_configuration_id=configuration,
    )


def source_snapshot(project, source, start, value, code):
    row = SourceSnapshot.objects.create(
        project=project,
        source=source,
        period_start=start,
        period_end=start,
        checksum=f"{source}-{start}",
        payload={},
    )
    MetricPoint.objects.create(
        snapshot=row,
        metric_code=code,
        numeric_value=value,
        unit=MetricPoint.Unit.COUNT,
    )
    return row


def test_report_form_offers_only_dates_complete_for_active_configurations():
    project = Project.objects.create(name="Complete", domain="complete.example")
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="1",
        selected_configurations=[{"id": "yandex"}, {"id": "google"}],
    )
    ranking(project, date(2026, 7, 1), "yandex")
    ranking(project, date(2026, 7, 2), "yandex")
    ranking(project, date(2026, 7, 2), "google")
    assert list(ReportCreateForm(project=project).fields["topvisor_dates"].choices) == [
        ("2026-07-02", "02.07.2026")
    ]


def test_selected_metrika_and_webmaster_periods_are_independent():
    project = Project.objects.create(name="Sources", domain="sources.example")
    m1 = source_snapshot(project, SourceSnapshot.Source.METRIKA, date(2026, 1, 1), 10, "visits")
    source_snapshot(project, SourceSnapshot.Source.METRIKA, date(2026, 2, 1), 999, "visits")
    m3 = source_snapshot(project, SourceSnapshot.Source.METRIKA, date(2026, 3, 1), 30, "visits")
    w1 = source_snapshot(
        project, SourceSnapshot.Source.WEBMASTER, date(2026, 4, 1), 40, "search_clicks"
    )
    w2 = source_snapshot(
        project, SourceSnapshot.Source.WEBMASTER, date(2026, 5, 1), 50, "search_clicks"
    )
    facts = build_source_facts(
        project=project,
        report_month=date(2026, 5, 1),
        selected_snapshot_ids={
            SourceSnapshot.Source.METRIKA: [str(m3.id), str(m1.id)],
            SourceSnapshot.Source.WEBMASTER: [str(w2.id), str(w1.id)],
        },
    )["sources"]
    assert [
        point["month"]
        for point in facts[SourceSnapshot.Source.METRIKA]["three_month_series"]["visits"]
    ] == [date(2026, 1, 1), date(2026, 3, 1)]
    assert facts[SourceSnapshot.Source.METRIKA]["normalized_changes"]["visits"].current == 30
    assert [
        point["month"]
        for point in facts[SourceSnapshot.Source.WEBMASTER]["three_month_series"]["search_clicks"]
    ] == [date(2026, 4, 1), date(2026, 5, 1)]
    assert "search_clicks" not in facts[SourceSnapshot.Source.METRIKA]["three_month_series"]
    assert "visits" not in facts[SourceSnapshot.Source.WEBMASTER]["three_month_series"]


def test_existing_report_gets_new_immutable_version_and_duplicate_post_is_blocked(client):
    user = get_user_model().objects.create_user("selection", password="password")
    project = Project.objects.create(name="Versions", domain="versions.example")
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="1",
        selected_configurations=[{"id": "google"}],
    )
    for day in (date(2026, 7, 1), date(2026, 7, 15), date(2026, 7, 31)):
        ranking(project, day, "google")
    client.force_login(user)
    list_url = reverse("reports:report-list", args=[project.id])
    create_url = reverse("reports:report-create", args=[project.id])
    token1 = client.get(list_url).context["form"].initial["submission_token"]
    first_data = {"submission_token": token1, "topvisor_dates": ["2026-07-01", "2026-07-15"]}
    client.post(create_url, first_data)
    report = Report.objects.get()
    first_payload = report.versions.get(number=1).snapshot.payload
    assert client.post(create_url, first_data).status_code == 302
    assert report.versions.count() == 1
    token2 = client.get(list_url).context["form"].initial["submission_token"]
    client.post(
        create_url, {"submission_token": token2, "topvisor_dates": ["2026-07-15", "2026-07-31"]}
    )
    report.refresh_from_db()
    assert report.versions.count() == 2
    assert report.versions.get(number=1).snapshot.payload == first_payload
    assert report.versions.get(number=2).snapshot.payload["source_selection"]["topvisor"][
        "selected_dates"
    ] == ["2026-07-15", "2026-07-31"]

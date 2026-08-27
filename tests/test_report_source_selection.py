from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.urls import reverse

from apps.metrics.models import MetricPoint, RankingSnapshot, SourceSnapshot
from apps.projects.models import Project
from apps.reports.forms import ReportCreateForm
from apps.reports.models import Report
from apps.reports.services import build_source_facts
from apps.topvisor.models import TopvisorProjectMapping
from apps.yandex.models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexWebmasterProjectMapping,
)

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
    form = ReportCreateForm(project=project)
    assert list(form.fields["yandex_dates"].choices) == [
        ("2026-07-02", "02.07.2026"),
        ("2026-07-01", "01.07.2026"),
    ]
    assert list(form.fields["google_dates"].choices) == [("2026-07-02", "02.07.2026")]


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


def test_form_defaults_to_three_report_month_source_periods_and_preserves_bound_selection():
    user = get_user_model().objects.create_user("source-defaults")
    project = Project.objects.create(name="Defaults", domain="defaults.example")
    ranking(project, date(2026, 7, 31), "google")
    connection = YandexConnection.objects.create(
        user=user, access_token_encrypted=b"token", active=True
    )
    YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="1",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    YandexWebmasterProjectMapping.objects.create(
        project=project,
        connection=connection,
        host_id="host",
        host_url="https://defaults.example/",
    )
    metrika = [
        source_snapshot(
            project, SourceSnapshot.Source.METRIKA, date(2026, month, 1), month, "visits"
        )
        for month in (4, 5, 6, 7)
    ]
    webmaster = [
        source_snapshot(
            project,
            SourceSnapshot.Source.WEBMASTER,
            date(2026, month, 1),
            month,
            "search_clicks",
        )
        for month in (5, 6, 7)
    ]

    form = ReportCreateForm(project=project)
    assert form["metrika_snapshots"].value() == [str(row.id) for row in metrika[1:]][::-1]
    assert form["webmaster_snapshots"].value() == [str(row.id) for row in webmaster][::-1]

    data = QueryDict(mutable=True)
    data.setlist("metrika_snapshots", [str(metrika[-1].id)])
    bound = ReportCreateForm(data, project=project)
    assert bound["metrika_snapshots"].value() == [str(metrika[-1].id)]
    assert not bound.is_valid()
    assert "выберите хотя бы один" in bound.errors["webmaster_snapshots"][0]


def test_metrika_search_segment_changes_traffic_but_not_source_breakdown():
    project = Project.objects.create(name="Segment", domain="segment.example")
    for month, base, search, all_traffic in (
        (6, 100, 40, 80),
        (7, 120, 55, 95),
    ):
        snapshot = source_snapshot(
            project,
            SourceSnapshot.Source.METRIKA,
            date(2026, month, 1),
            base,
            "visits",
        )
        for code, value in (
            ("segment_search_humans_visits", search),
            ("segment_all_humans_visits", all_traffic),
            ("source_search_visits", 33 + month),
        ):
            MetricPoint.objects.create(
                snapshot=snapshot,
                metric_code=code,
                numeric_value=value,
                unit=MetricPoint.Unit.COUNT,
            )
    selected = list(SourceSnapshot.objects.filter(project=project).values_list("id", flat=True))

    search_facts = build_source_facts(
        project=project,
        report_month=date(2026, 7, 1),
        selected_snapshot_ids={SourceSnapshot.Source.METRIKA: selected},
        display_options={"metrika_search_segment": True, "metrika_robotness": "humans"},
    )["sources"][SourceSnapshot.Source.METRIKA]
    all_facts = build_source_facts(
        project=project,
        report_month=date(2026, 7, 1),
        selected_snapshot_ids={SourceSnapshot.Source.METRIKA: selected},
        display_options={"metrika_search_segment": False, "metrika_robotness": "humans"},
    )["sources"][SourceSnapshot.Source.METRIKA]

    assert search_facts["normalized_changes"]["visits"].current == Decimal("55")
    assert all_facts["normalized_changes"]["visits"].current == Decimal("95")
    assert (
        search_facts["normalized_changes"]["source_search_visits"].current
        == all_facts["normalized_changes"]["source_search_visits"].current
    )


def test_single_source_period_has_no_previous_value_or_zero_change():
    project = Project.objects.create(name="Single", domain="single.example")
    snapshot = source_snapshot(
        project, SourceSnapshot.Source.METRIKA, date(2026, 7, 1), 42, "visits"
    )
    change = build_source_facts(
        project=project,
        report_month=date(2026, 7, 1),
        selected_snapshot_ids={SourceSnapshot.Source.METRIKA: [str(snapshot.id)]},
    )["sources"][SourceSnapshot.Source.METRIKA]["normalized_changes"]["visits"]
    assert change.current is not None
    assert change.previous is None
    assert change.absolute is None


def test_report_page_keeps_source_period_controls_inside_each_source_card(client):
    user = get_user_model().objects.create_user("source-ui")
    project = Project.objects.create(name="Source UI", domain="source-ui.example")
    ranking(project, date(2026, 7, 31), "google")
    connection = YandexConnection.objects.create(
        user=user,
        access_token_encrypted=b"token",
        active=True,
    )
    YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="1",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    YandexWebmasterProjectMapping.objects.create(
        project=project,
        connection=connection,
        host_id="host",
        host_url="https://source-ui.example/",
    )
    for month in (5, 6, 7):
        source_snapshot(
            project,
            SourceSnapshot.Source.METRIKA,
            date(2026, month, 1),
            month,
            "visits",
        )
        source_snapshot(
            project,
            SourceSnapshot.Source.WEBMASTER,
            date(2026, month, 1),
            month,
            "search_clicks",
        )
    client.force_login(user)

    response = client.get(reverse("reports:report-list", args=[project.id]))
    html = response.content.decode()

    metrika_start = html.index('data-source-label="Метрика"')
    webmaster_start = html.index('data-source-label="Вебмастер"')
    metrika_card = html[metrika_start:webmaster_start]
    webmaster_card = html[webmaster_start:]
    assert 'value="2026-05" data-period-start' in metrika_card
    assert 'value="2026-07" data-period-end' in metrika_card
    assert "Метрика: выбрано 3 периода." in metrika_card
    assert 'name="metrika_snapshots"' in metrika_card
    assert 'name="webmaster_snapshots"' not in metrika_card
    assert "Вебмастер: выбрано 3 периода." in webmaster_card
    assert 'name="webmaster_snapshots"' in webmaster_card
    assert "Перед созданием ежемесячного отчёта синхронизируйте оба источника" in html
    assert 'form="sync-metrika-form"' in metrika_card
    assert 'form="sync-webmaster-form"' in webmaster_card
    assert 'action="' + reverse("yandex:sync", args=[project.id]) + '"' in html
    assert 'action="' + reverse("yandex:sync-webmaster", args=[project.id]) + '"' in html
    assert html.count('name="return_to_reports" value="1"') == 2
    assert html.count('value="2026-07" data-sync-month-for=') == 2
    assert "Не определено</label>" not in html
    assert "Область не определена</label>" not in html
    assert list(response.context["form"]["metrika_bar_search_engines"].value()) == [
        "google",
        "yandex",
    ]
    assert 'value="bing"' in html and 'value="yahoo"' in html


def test_report_page_sync_buttons_return_to_report_builder(client, monkeypatch):
    user = get_user_model().objects.create_user("source-sync")
    project = Project.objects.create(name="Source Sync", domain="source-sync.example")
    connection = YandexConnection.objects.create(
        user=user,
        access_token_encrypted=b"token",
        active=True,
    )
    metrika_mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="1",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    webmaster_mapping = YandexWebmasterProjectMapping.objects.create(
        project=project,
        connection=connection,
        host_id="host",
        host_url="https://source-sync.example/",
    )
    success = SimpleNamespace(status="success", Status=SimpleNamespace(SUCCESS="success"))
    monkeypatch.setattr("apps.yandex.views.sync_metrika", lambda **kwargs: success)
    monkeypatch.setattr("apps.yandex.views.sync_webmaster", lambda **kwargs: success)
    client.force_login(user)
    expected = reverse("reports:report-list", args=[project.id])

    metrika_response = client.post(
        reverse("yandex:sync", args=[project.id]),
        {"month": "2026-07", "return_to_reports": "1"},
    )
    webmaster_response = client.post(
        reverse("yandex:sync-webmaster", args=[project.id]),
        {"month": "2026-07", "return_to_reports": "1"},
    )

    assert metrika_response.url == expected
    assert webmaster_response.url == expected
    assert Report.objects.filter(project=project, report_month=date(2026, 7, 1)).exists()
    assert metrika_mapping.project_id == project.id
    assert webmaster_mapping.project_id == project.id


def test_report_page_ajax_sync_returns_periods_without_reload(client, monkeypatch):
    user = get_user_model().objects.create_user("source-ajax-sync")
    project = Project.objects.create(name="Source Ajax Sync", domain="source-ajax.example")
    connection = YandexConnection.objects.create(
        user=user,
        access_token_encrypted=b"token",
        active=True,
    )
    YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="1",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    success = SimpleNamespace(status="success", Status=SimpleNamespace(SUCCESS="success"))

    def fake_sync(**_kwargs):
        for month in (5, 6, 7):
            source_snapshot(
                project,
                SourceSnapshot.Source.METRIKA,
                date(2026, month, 1),
                month,
                "visits",
            )
        return success

    monkeypatch.setattr("apps.yandex.views.sync_metrika", fake_sync)
    client.force_login(user)
    response = client.post(
        reverse("yandex:sync", args=[project.id]),
        {"month": "2026-07", "return_to_reports": "1"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert [period["month"] for period in response.json()["periods"]] == [
        "2026-07",
        "2026-06",
        "2026-05",
    ]
    assert not Report.objects.filter(project=project).exists()


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
    first_data = {"submission_token": token1, "google_dates": ["2026-07-01", "2026-07-15"]}
    client.post(create_url, first_data)
    report = Report.objects.get()
    first_payload = report.versions.get(number=1).snapshot.payload
    assert client.post(create_url, first_data).status_code == 302
    assert report.versions.count() == 1
    token2 = client.get(list_url).context["form"].initial["submission_token"]
    client.post(
        create_url, {"submission_token": token2, "google_dates": ["2026-07-15", "2026-07-31"]}
    )
    report.refresh_from_db()
    assert report.versions.count() == 2
    assert report.versions.get(number=1).snapshot.payload == first_payload
    assert report.versions.get(number=2).snapshot.payload["source_selection"]["topvisor"]["google"][
        "selected_dates"
    ] == ["2026-07-15", "2026-07-31"]

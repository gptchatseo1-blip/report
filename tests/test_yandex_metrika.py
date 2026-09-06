import hashlib
import io
import json
import urllib.error
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.metrics.models import MetricPoint, SourceSnapshot
from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import ProjectReportSettings, Report
from apps.reports.services import create_report_version
from apps.yandex.client import MetrikaClient, YandexAPIError
from apps.yandex.crypto import decrypt_token, encrypt_token
from apps.yandex.models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexMetrikaSyncRun,
    YandexOAuthState,
)
from apps.yandex.services import prune_sync_runs, sync_metrika
from apps.yandex.views import consume_oauth_state

pytestmark = pytest.mark.django_db
FERNET_KEY = Fernet.generate_key().decode()
TEST_ACCESS_TOKEN = "-".join(("test", "access", "credential"))
TEST_REFRESH_TOKEN = "-".join(("test", "refresh", "credential"))
TEST_CLIENT_SECRET = "-".join(("test", "client", "credential"))


@pytest.fixture
def yandex_settings(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    settings.YANDEX_CLIENT_ID = "client-id"
    settings.YANDEX_CLIENT_SECRET = TEST_CLIENT_SECRET
    settings.YANDEX_REDIRECT_URI = "https://reports.example/yandex/oauth/callback/"
    settings.YANDEX_MAX_RETRIES = 2
    return settings


@pytest.fixture
def identity():
    user = get_user_model().objects.create_user("owner", password="password")
    project = Project.objects.create(name="Site", domain="site.example")
    return user, project


def make_connection(user):
    return YandexConnection.objects.create(
        user=user,
        access_token_encrypted=encrypt_token(TEST_ACCESS_TOKEN),
        refresh_token_encrypted=encrypt_token(TEST_REFRESH_TOKEN),
    )


def make_state(user, project, raw="state", session="session"):
    return YandexOAuthState.objects.create(
        digest=hashlib.sha256(raw.encode()).hexdigest(),
        user=user,
        session_key=session,
        project=project,
    )


def test_oauth_state_valid_expired_foreign_and_reused(identity):
    user, project = identity
    state = make_state(user, project)
    assert consume_oauth_state(raw="state", user=user, session_key="session").pk == state.pk
    assert consume_oauth_state(raw="state", user=user, session_key="session") is None

    expired = make_state(user, project, raw="expired")
    YandexOAuthState.objects.filter(pk=expired.pk).update(
        created_at=timezone.now() - timedelta(minutes=11)
    )
    assert consume_oauth_state(raw="expired", user=user, session_key="session") is None

    stranger = get_user_model().objects.create_user("stranger")
    make_state(user, project, raw="foreign")
    assert consume_oauth_state(raw="foreign", user=stranger, session_key="session") is None
    assert consume_oauth_state(raw="foreign", user=user, session_key="wrong") is None


def test_atomic_state_has_single_winner(identity):
    user, project = identity
    make_state(user, project, raw="race")
    winners = [
        consume_oauth_state(raw="race", user=user, session_key="session"),
        consume_oauth_state(raw="race", user=user, session_key="session"),
    ]
    assert sum(item is not None for item in winners) == 1


def test_tokens_are_only_encrypted_in_database(identity, yandex_settings):
    connection = make_connection(identity[0])
    connection.refresh_from_db()
    stored = bytes(connection.access_token_encrypted)
    assert TEST_ACCESS_TOKEN.encode() not in stored
    assert TEST_REFRESH_TOKEN.encode() not in bytes(connection.refresh_token_encrypted)
    assert decrypt_token(stored) == TEST_ACCESS_TOKEN


def test_counter_selection_uses_server_response_and_site2(
    client, identity, yandex_settings, monkeypatch
):
    user, project = identity
    connection = make_connection(user)
    client.force_login(user)
    monkeypatch.setattr(
        MetrikaClient,
        "counter",
        lambda self, counter_id: {
            "id": int(counter_id),
            "name": "Canonical name",
            "site2": {"site": "site.example"},
        },
    )
    response = client.post(
        reverse("yandex:select-counter", args=[project.id]),
        {
            "connection_id": connection.id,
            "counter_id": "42",
            "counter_name": "Forged",
            "counter_domain": "evil.example",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert "Счётчик Яндекс.Метрики сохранён." in response.content.decode()
    mapping = project.yandex_metrika_mapping
    assert (mapping.counter_name, mapping.counter_domain) == ("Canonical name", "site.example")


def test_counter_domain_mismatch_requires_confirmation(
    client, identity, yandex_settings, monkeypatch
):
    user, project = identity
    connection = make_connection(user)
    client.force_login(user)
    monkeypatch.setattr(
        MetrikaClient,
        "counter",
        lambda *_: {"id": 42, "name": "Mirror", "site": "mirror.example"},
    )
    url = reverse("yandex:select-counter", args=[project.id])
    data = {"connection_id": connection.id, "counter_id": "42"}
    client.post(url, data)
    assert not YandexMetrikaProjectMapping.objects.exists()
    client.post(url, {**data, "confirm_domain_mismatch": "on"})
    assert project.yandex_metrika_mapping.domain_mismatch_confirmed is True


def test_unavailable_counter_and_goals_are_safe(client, identity, yandex_settings, monkeypatch):
    user, project = identity
    connection = make_connection(user)
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="42",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    client.force_login(user)
    secret = TEST_ACCESS_TOKEN
    monkeypatch.setattr(
        MetrikaClient, "counter", lambda *_: (_ for _ in ()).throw(YandexAPIError("Безопасно"))
    )
    response = client.post(
        reverse("yandex:select-counter", args=[project.id]),
        {"connection_id": connection.id, "counter_id": "42"},
        follow=True,
    )
    assert response.status_code == 200 and secret not in response.content.decode()
    monkeypatch.setattr(
        MetrikaClient, "goals", lambda *_: (_ for _ in ()).throw(YandexAPIError("Безопасно"))
    )
    response = client.post(reverse("yandex:select-goals", args=[project.id]), follow=True)
    assert response.status_code == 200 and secret not in response.content.decode()
    assert mapping.selected_goals == []


def test_goal_selection_uses_available_goals(client, identity, yandex_settings, monkeypatch):
    user, project = identity
    connection = make_connection(user)
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="42",
        counter_name="C",
        counter_domain=project.domain,
    )
    client.force_login(user)
    monkeypatch.setattr(
        MetrikaClient,
        "goals",
        lambda *_: iter([{"id": 7, "name": "Order", "type": "Action"}]),
    )
    response = client.post(reverse("yandex:select-goals", args=[project.id]), {"goals": ["7"]})
    assert response.status_code == 302
    mapping.refresh_from_db()
    assert mapping.selected_goals == [
        {"id": "7", "name": "Order", "label": "Order", "type": "Action"}
    ]


def test_goals_use_compact_picker_with_selected_count(
    client, identity, yandex_settings, monkeypatch
):
    user, project = identity
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=make_connection(user),
        counter_id="42",
        counter_name="C",
        counter_domain=project.domain,
        selected_goals=[{"id": "7", "name": "Order", "label": "Order"}],
    )
    client.force_login(user)
    monkeypatch.setattr(
        MetrikaClient,
        "goals",
        lambda *_: [
            {"id": 7, "name": "Order"},
            {"id": 8, "name": "Callback"},
        ],
    )

    connection_html = client.get(reverse("yandex:connection", args=[project.id])).content.decode()
    html = client.get(reverse("reports:report-list", args=[project.id])).content.decode()

    assert mapping.counter_id in connection_html
    assert "Выбрать цели" not in connection_html
    assert "data-report-goal-picker" in html
    assert "compact-goal-options" in html
    assert "Выбрать цели · выбрано <span data-goal-count>1</span>" in html
    assert 'value="7" form="report-metrika-goals-form" checked' in html
    assert 'value="8"' in html


def test_metrika_run_can_only_be_deleted_by_post_and_keeps_snapshots(
    client, identity, yandex_settings
):
    user, project = identity
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=make_connection(user),
        counter_id="42",
        counter_name="C",
        counter_domain=project.domain,
    )
    run = YandexMetrikaSyncRun.objects.create(
        mapping=mapping,
        report_month=date(2026, 3, 1),
        status=YandexMetrikaSyncRun.Status.SUCCESS,
    )
    snapshot = SourceSnapshot.objects.create(
        project=project,
        source=SourceSnapshot.Source.METRIKA,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        checksum="kept",
        payload={},
    )
    url = reverse("yandex:delete-metrika-run", args=[project.id, run.id])
    client.force_login(user)

    assert client.get(url).status_code == 405
    response = client.post(url)

    assert response.status_code == 302
    assert not YandexMetrikaSyncRun.objects.filter(pk=run.id).exists()
    assert SourceSnapshot.objects.filter(pk=snapshot.pk).exists()


def test_project_cleanup_removes_selected_source_month_and_keeps_report_snapshot(
    client, identity, yandex_settings
):
    user, project = identity
    mapping = mapping_with_goal(identity, yandex_settings)
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    report = Report.objects.create(project=project, report_month=date(2026, 3, 1))
    version = create_report_version(report=report, created_by=user)
    frozen_snapshot_id = version.snapshot.id
    client.force_login(user)
    url = reverse("reports:source-history-clear", args=[project.id])

    response = client.post(
        url,
        {"source": "yandex_metrika", "action": "delete_selected", "months": ["2026-01"]},
    )

    assert response.status_code == 302
    assert not SourceSnapshot.objects.filter(
        project=project, period_start=date(2026, 1, 1)
    ).exists()
    assert (
        Report.objects.get(pk=report.pk).versions.get(pk=version.pk).snapshot.id
        == frozen_snapshot_id
    )
    client.post(url, {"source": "yandex_metrika", "action": "delete_runs"})
    assert not YandexMetrikaSyncRun.objects.filter(mapping=mapping).exists()


def test_sync_log_retention_does_not_delete_monthly_snapshots(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    old = YandexMetrikaSyncRun.objects.create(mapping=mapping, report_month=date(2025, 1, 1))
    recent = YandexMetrikaSyncRun.objects.create(mapping=mapping, report_month=date(2026, 9, 1))
    YandexMetrikaSyncRun.objects.filter(pk=old.pk).update(
        started_at=timezone.now() - timedelta(days=400)
    )
    snapshot = SourceSnapshot.objects.create(
        project=mapping.project,
        source=SourceSnapshot.Source.METRIKA,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        checksum="kept",
        payload={},
    )
    ProjectReportSettings.objects.create(
        project=mapping.project, values={"sync_log_retention_months": "12"}
    )

    prune_sync_runs(mapping.project)

    assert not YandexMetrikaSyncRun.objects.filter(pk=old.pk).exists()
    assert YandexMetrikaSyncRun.objects.filter(pk=recent.pk).exists()
    assert SourceSnapshot.objects.filter(pk=snapshot.pk).exists()


class FakeMetrika:
    def __init__(self, fail_call=None):
        self.calls = []
        self.fail_call = fail_call

    def stat(self, **params):
        self.calls.append(params)
        if self.fail_call == len(self.calls):
            raise YandexAPIError("Provider unavailable")
        metrics = params["metrics"]
        if params.get("dimensions") == "ym:s:regionArea,ym:s:regionCity":
            return {
                "data": [
                    {
                        "dimensions": [
                            {"id": "1", "name": "Москва и Московская область"},
                            {"id": "213", "name": "Москва"},
                        ],
                        "metrics": [40],
                    },
                    {
                        "dimensions": [
                            {"id": "2", "name": "Санкт-Петербург и Ленинградская область"},
                            {"id": "2", "name": "Санкт-Петербург"},
                        ],
                        "metrics": [20],
                    },
                    {
                        "dimensions": [
                            {"id": "0", "name": "Область не определена"},
                            {"id": "0", "name": "Не определено"},
                        ],
                        "metrics": [5],
                    },
                    {
                        "dimensions": [
                            {"id": "1", "name": "Москва и Московская область"},
                            {"id": "0", "name": "Не определено"},
                        ],
                        "metrics": [3],
                    },
                ]
            }
        if params.get("dimensions"):
            return {
                "data": [{"dimensions": [{"id": "organic", "name": "Search"}], "metrics": [60]}]
            }
        if "goal" in metrics:
            return {"data": [{"metrics": [3, 2.5]}]}
        return {
            "data": [{"metrics": [100, 80, 60, 10, 2.1, 90]}],
            "sampled": True,
            "sample_share": 0.75,
        }


def mapping_with_goal(identity, yandex_settings):
    user, project = identity
    return YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=make_connection(user),
        counter_id="42",
        counter_name="Counter",
        counter_domain=project.domain,
        selected_goals=[{"id": "7", "name": "Renamed", "label": "Client label"}],
    )


def test_sync_three_months_goals_sources_sampling_and_idempotency(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    api = FakeMetrika()
    assert (
        sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=api).status == "success"
    )
    snapshots = SourceSnapshot.objects.filter(project=mapping.project).order_by("period_start")
    assert list(snapshots.values_list("period_start", flat=True)) == [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
    ]
    assert snapshots[0].sampling == {"sampled": True, "share": 0.75}
    assert snapshots[0].provenance["counter_id"] == "42"
    points = {p.metric_code: p for p in snapshots[0].metrics.all()}
    assert points["source_search_visits"].numeric_value == 60
    assert points["goal_7_reaches"].dimensions["label"] == "Client label"
    assert points["geography_moscow_visits"].numeric_value == 40
    assert points["geography_saint_petersburg_visits"].numeric_value == 20
    assert points["geography_undefined_visits"].numeric_value == 3
    assert points["geography_area_undefined_visits"].numeric_value == 5
    source_details = snapshots[0].payload["traffic_source_details"]
    assert source_details[0]["code"] == "search"
    assert source_details[0]["visits"] == "60"
    assert {"users", "bounce_rate", "page_depth", "avg_visit_duration_seconds"} <= set(
        source_details[0]
    )
    assert snapshots[2].payload["traffic_source_quarter_details"][0]["visits"] == "60"
    cached_api = FakeMetrika()
    cached = sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=cached_api)
    assert cached_api.calls
    assert (cached.fetched_period_count, cached.reused_period_count) == (1, 2)
    assert SourceSnapshot.objects.filter(project=mapping.project).count() == 3
    assert MetricPoint.objects.filter(snapshot__project=mapping.project).count() == len(points) * 3


def test_default_search_segment_uses_last_significant_attribution(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    api = FakeMetrika()

    run = sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=api)

    assert run.status == "success"
    assert all(call.get("attribution") == "cross_device_last_significant" for call in api.calls)
    assert any(
        call.get("dimensions") == "ym:s:<attribution>TrafficSource" for call in api.calls
    )
    assert any(
        call.get("dimensions") == "ym:s:<attribution>SearchEngineRoot" for call in api.calls
    )
    assert any(
        call.get("dimensions")
        == "ym:s:<attribution>SearchEngineRoot,ym:s:startURL"
        for call in api.calls
    )


def test_late_failure_preserves_existing_snapshots(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    before = list(
        SourceSnapshot.objects.filter(project=mapping.project).values_list("id", "checksum")
    )
    # Force refresh and fail after two complete months; old snapshots stay intact.
    run = sync_metrika(
        mapping=mapping,
        report_month=date(2026, 3, 1),
        client=FakeMetrika(fail_call=41),
        force_refresh=True,
    )
    assert run.status == "failed"
    assert (
        list(SourceSnapshot.objects.filter(project=mapping.project).values_list("id", "checksum"))
        == before
    )


def test_goal_requests_are_batched_and_rate_limit_is_reported(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    mapping.selected_goals = [
        {"id": str(index), "name": f"Goal {index}", "label": f"Goal {index}"}
        for index in range(1, 27)
    ]
    mapping.save(update_fields=["selected_goals", "updated_at"])
    api = FakeMetrika()

    run = sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=api)

    assert run.status == run.Status.SUCCESS
    assert len(api.calls) == 109
    limited = sync_metrika(
        mapping=mapping,
        report_month=date(2026, 3, 1),
        client=FakeMetrikaRateLimited(),
        force_refresh=True,
    )
    assert limited.status == limited.Status.FAILED
    assert "Лимит запросов" in limited.error_message


class FakeMetrikaRateLimited(FakeMetrika):
    def stat(self, **params):
        raise YandexAPIError("safe", http_status=429)


class FakeMetrikaWithDeletedGoal(FakeMetrika):
    def stat(self, **params):
        metrics = params["metrics"]
        if "goal13" in metrics:
            raise YandexAPIError("safe", http_status=400, error_code="invalid_parameter")
        return super().stat(**params)


def test_deleted_goal_is_skipped_without_losing_other_goals(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    mapping.selected_goals.append({"id": "13", "name": "Deleted", "label": "Deleted"})
    mapping.save(update_fields=["selected_goals", "updated_at"])

    run = sync_metrika(
        mapping=mapping,
        report_month=date(2026, 3, 1),
        client=FakeMetrikaWithDeletedGoal(),
    )

    codes = set(SourceSnapshot.objects.first().metrics.values_list("metric_code", flat=True))
    assert run.status == run.Status.SUCCESS
    assert run.unavailable_goal_ids == ["13"]
    assert "goal_7_visits" in codes
    assert not any("goal_13" in code for code in codes)


def test_report_snapshot_contains_safe_source_metadata(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    report = Report.objects.create(project=mapping.project, report_month=date(2026, 3, 1))
    version = create_report_version(report=report, created_by=identity[0])
    source = version.snapshot.payload["source_snapshots"][0]
    assert {
        "retrieval_method",
        "checksum",
        "retrieved_at",
        "provenance",
        "sampling",
        "contains_sensitive_data",
    } <= source.keys()
    serialized = json.dumps(version.snapshot.payload)
    assert all(
        secret not in serialized
        for secret in (
            TEST_ACCESS_TOKEN,
            TEST_REFRESH_TOKEN,
            TEST_CLIENT_SECRET,
            "Authorization",
        )
    )


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def http_error(code):
    return urllib.error.HTTPError("https://safe.example", code, "error", {}, io.BytesIO())


def test_pagination_and_retry_429_5xx(identity, yandex_settings):
    connection = make_connection(identity[0])
    calls = []
    outcomes = [http_error(429), http_error(503), Response({"counters": [{"id": 1}]})]

    def opener(*args, **kwargs):
        calls.append(args[0])
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    api = MetrikaClient(connection, opener=opener, sleep=lambda _: None)
    assert list(api.counters()) == [{"id": 1}]
    assert len(calls) == 3


def test_counter_pagination_advances_offset_by_received_rows(identity, yandex_settings):
    connection = make_connection(identity[0])
    offsets = []
    api = MetrikaClient(connection)

    def request(path, params, **kwargs):
        offsets.append(params["offset"])
        return (
            {"counters": [{"id": i} for i in range(2)]}
            if params["offset"] == 1
            else {"counters": []}
        )

    api._request = request
    assert len(list(api._pages("x", "counters", page_size=2))) == 2
    assert offsets == [1, 3]


def test_goals_uses_single_request_without_pagination(identity, yandex_settings):
    api = MetrikaClient(make_connection(identity[0]))
    calls = []

    def request(path, params=None, **kwargs):
        calls.append((path, params))
        return {"goals": [{"id": 7, "name": "Order"}]}

    api._request = request
    assert api.goals("42") == [{"id": 7, "name": "Order"}]
    assert calls == [("management/v1/counter/42/goals", None)]


def test_401_refreshes_only_once(identity, yandex_settings, monkeypatch):
    connection = make_connection(identity[0])
    refreshes = []
    api = MetrikaClient(
        connection, opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(http_error(401))
    )
    monkeypatch.setattr(api, "_refresh", lambda: refreshes.append(1))
    with pytest.raises(YandexAPIError):
        api._request("management/v1/counters")
    assert refreshes == [1]


def test_admin_and_html_never_render_tokens(client, identity, yandex_settings, monkeypatch):
    user, project = identity
    make_connection(user)
    client.force_login(user)
    monkeypatch.setattr(MetrikaClient, "counters", lambda *_: iter([]))
    body = client.get(reverse("yandex:connection", args=[project.id])).content.decode()
    assert TEST_ACCESS_TOKEN not in body and TEST_REFRESH_TOKEN not in body
    from apps.yandex.admin import YandexConnectionAdmin

    assert "access_token_encrypted" in YandexConnectionAdmin.exclude
    assert "refresh_token_encrypted" in YandexConnectionAdmin.exclude


def test_connection_uses_one_compact_counter_select_and_no_duplicate_oauth_link(
    client, identity, yandex_settings, monkeypatch
):
    user, project = identity
    make_connection(user)
    client.force_login(user)
    monkeypatch.setattr(
        MetrikaClient,
        "counters",
        lambda *_: iter(
            [
                {"id": 42, "name": "Site", "site": "site.example"},
                {"id": 43, "name": "Other", "site": "other.example"},
            ]
        ),
    )

    html = client.get(reverse("yandex:connection", args=[project.id])).content.decode()

    assert "2. Счётчик Яндекс.Метрика" in html
    assert "Настройки OAuth" not in html
    assert html.count(reverse("yandex:select-counter", args=[project.id])) == 1
    assert '<select id="counter-id"' in html
    assert 'value="42" data-domain-mismatch="false"' in html
    assert 'value="43" data-domain-mismatch="true"' in html
    assert "Счётчик относится к другому домену" in html


def test_version_docx_xlsx_exports_never_call_live_metrika(
    identity, yandex_settings, settings, tmp_path, monkeypatch
):
    mapping = mapping_with_goal(identity, yandex_settings)
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    report = Report.objects.create(project=mapping.project, report_month=date(2026, 3, 1))
    version = create_report_version(report=report, created_by=identity[0])
    settings.MEDIA_ROOT = tmp_path

    def live_api_forbidden(*args, **kwargs):
        raise AssertionError("export attempted to call live Metrika API")

    monkeypatch.setattr(MetrikaClient, "_request", live_api_forbidden)
    docx = generate_artifact(version=version, artifact_type="docx", is_draft=True)
    xlsx = generate_artifact(version=version, artifact_type="xlsx", is_draft=True)
    assert docx.file.size > 0
    assert xlsx.file.size > 0

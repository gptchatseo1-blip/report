import hashlib
import io
import json
import urllib.error
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.metrics.models import MetricPoint, SourceSnapshot
from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import Report
from apps.reports.services import create_report_version
from apps.yandex.client import MetrikaClient, YandexAPIError
from apps.yandex.crypto import decrypt_token, encrypt_token
from apps.yandex.models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexOAuthState,
)
from apps.yandex.services import sync_metrika
from apps.yandex.views import consume_oauth_state

pytestmark = pytest.mark.django_db
FERNET_KEY = "rN4-j6VCo2PxKB9RCqaVvzwuR4mqUUqe5xzHIZfyg3A="


@pytest.fixture
def yandex_settings(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    settings.YANDEX_CLIENT_ID = "client-id"
    settings.YANDEX_CLIENT_SECRET = "client-secret"
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
        access_token_encrypted=encrypt_token("access-secret"),
        refresh_token_encrypted=encrypt_token("refresh-secret"),
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
    assert b"access-secret" not in stored
    assert b"refresh-secret" not in bytes(connection.refresh_token_encrypted)
    assert decrypt_token(stored) == "access-secret"


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
    )
    assert response.status_code == 302
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
    secret = "access-secret"
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
    monkeypatch.setattr(MetrikaClient, "goals", lambda *_: iter([{"id": 7, "name": "Order"}]))
    response = client.post(reverse("yandex:select-goals", args=[project.id]), {"goals": ["7"]})
    assert response.status_code == 302
    mapping.refresh_from_db()
    assert mapping.selected_goals == [{"id": "7", "name": "Order", "label": "Order"}]


class FakeMetrika:
    def __init__(self, fail_call=None):
        self.calls = []
        self.fail_call = fail_call

    def stat(self, **params):
        self.calls.append(params)
        if self.fail_call == len(self.calls):
            raise YandexAPIError("Provider unavailable")
        metrics = params["metrics"]
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
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    assert SourceSnapshot.objects.filter(project=mapping.project).count() == 3
    assert MetricPoint.objects.filter(snapshot__project=mapping.project).count() == len(points) * 3


def test_late_failure_preserves_existing_snapshots(identity, yandex_settings):
    mapping = mapping_with_goal(identity, yandex_settings)
    sync_metrika(mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika())
    before = list(
        SourceSnapshot.objects.filter(project=mapping.project).values_list("id", "checksum")
    )
    # Four calls per month with one selected goal: fail in the third month's first request.
    run = sync_metrika(
        mapping=mapping, report_month=date(2026, 3, 1), client=FakeMetrika(fail_call=9)
    )
    assert run.status == "failed"
    assert (
        list(SourceSnapshot.objects.filter(project=mapping.project).values_list("id", "checksum"))
        == before
    )


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
        for secret in ("access-secret", "refresh-secret", "client-secret", "Authorization")
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
    assert "access-secret" not in body and "refresh-secret" not in body
    from apps.yandex.admin import YandexConnectionAdmin

    assert "access_token_encrypted" in YandexConnectionAdmin.exclude
    assert "refresh_token_encrypted" in YandexConnectionAdmin.exclude


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

import io
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Project
from apps.yandex.client import MetrikaClient, YandexAPIError
from apps.yandex.crypto import encrypt_token
from apps.yandex.models import (
    MetrikaQuotaState,
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexMetrikaSyncRun,
)
from apps.yandex.quotas import MetrikaQuota, QuotaBlocked, error_type, quota_details, quota_message
from apps.yandex.services import month_end, sync_metrika

pytestmark = pytest.mark.django_db


class Clock:
    def __init__(self):
        self.value = timezone.now()
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


def limited(code, *, retry_after=None, message="Limit reached"):
    payload = {"code": 429, "errors": [{"error_type": code, "message": message}]}
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "https://api-metrika.yandex.net/",
        429,
        "Limit",
        headers,
        io.BytesIO(json.dumps(payload).encode()),
    )


@pytest.fixture
def account(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.YANDEX_METRIKA_MIN_INTERVAL_SECONDS = 2
    user = get_user_model().objects.create_user("quota-owner")
    return YandexConnection.objects.create(
        user=user,
        account_id="42",
        account_login="owner",
        access_token_encrypted=encrypt_token("test-private-token"),
    )


def test_pacing_is_shared_by_clients_projects_and_restarts(account):
    clock = Clock()
    gate = MetrikaQuota(clock=clock.now)
    assert gate.acquire(account, "stat/v1/data", {"ids": "1"}) == (0, "")
    assert gate.acquire(account, "stat/v1/data", {"ids": "2"})[0] == 2
    clock.sleep(2)
    assert MetrikaQuota(clock=clock.now).acquire(account, "stat/v1/data", {"ids": "2"}) == (0, "")
    assert gate.acquire(account, "stat/v1/data", {"ids": "1"})[0] == 2
    assert MetrikaQuotaState.objects.count() == 1


def test_service_stays_below_200_requests_per_five_minutes(account):
    clock = Clock()
    gate = MetrikaQuota(clock=clock.now)
    sent = []
    for index in range(240):
        delay, _ = gate.acquire(account, "stat/v1/data", {"ids": str(index % 3)})
        if delay:
            clock.sleep(delay)
            assert gate.acquire(account, "stat/v1/data", {"ids": str(index % 3)})[0] == 0
        sent.append(clock.now())
        assert sum(clock.now() - timedelta(minutes=5) < when <= clock.now() for when in sent) <= 150


def test_temporary_quota_retries_same_request_and_counts_http_attempts(account):
    clock, events, urls = Clock(), [], []

    def opener(request, **_kwargs):
        urls.append(request.full_url)
        if len(urls) == 1:
            raise limited("quota_requests_by_uid", message="Queries per 5 minutes exceeded")
        return Response({"totals": [239]})

    api = MetrikaClient(account, opener=opener, sleep=clock.sleep, clock=clock.now)
    api.progress = lambda event, **values: events.append((event, values))
    assert api.stat(ids="1", metrics="ym:s:visits") == {"totals": [239]}
    assert api.request_count == 2
    assert api.retry_count == 1
    assert urls[0] == urls[1]
    assert sum(clock.sleeps) >= 305
    assert max(clock.sleeps) <= 30
    assert any(event == "heartbeat" for event, _ in events)
    assert "test-private-token" not in repr(events)


def test_daily_quota_is_not_retried_and_is_shared_with_reconnected_account(account):
    clock = Clock()

    def opener(*_args, **_kwargs):
        raise limited("quota_requests_by_uid")

    api = MetrikaClient(account, opener=opener, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(YandexAPIError) as first:
        api.stat(ids="1", metrics="ym:s:visits")
    assert first.value.quota_daily is True
    assert first.value.retry_at.astimezone(UTC).hour == 0
    assert api.request_count == 1 and api.retry_count == 0
    assert clock.sleeps == []
    other = YandexConnection.objects.create(
        user=account.user,
        account_id=account.account_id,
        access_token_encrypted=account.access_token_encrypted,
    )
    fresh = MetrikaClient(other, opener=opener, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(YandexAPIError) as second:
        fresh.stat(ids="2", metrics="ym:s:visits")
    assert fresh.request_count == 0
    assert second.value.retry_at == first.value.retry_at
    assert "суточная" in quota_message(second.value.error_code, second.value.retry_at)
    assert "03:00:01 МСК" in quota_message(second.value.error_code, second.value.retry_at)


def test_counter_daily_quota_does_not_block_other_counter(account):
    clock = Clock()
    gate = MetrikaQuota(clock=clock.now)
    gate.defer(
        account,
        {"ids": "1"},
        "quota_requests_by_counter_id",
        clock.now() + timedelta(hours=1),
        daily=True,
    )
    with pytest.raises(QuotaBlocked):
        gate.acquire(account, "stat/v1/data", {"ids": "1"})
    assert gate.acquire(account, "stat/v1/data", {"ids": "2"}) == (0, "")


def test_web_request_does_not_block_for_a_background_cooldown(account):
    clock = Clock()
    gate = MetrikaQuota(clock=clock.now)
    gate.defer(
        account,
        {"ids": "1"},
        "quota_parallel_requests",
        clock.now() + timedelta(minutes=5),
        daily=False,
    )
    api = MetrikaClient(account, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(YandexAPIError) as blocked:
        api.stat(ids="1", metrics="ym:s:visits")
    assert blocked.value.quota_daily is False
    assert api.request_count == 0
    assert clock.sleeps == []


def test_retry_after_http_date_and_error_array_are_understood():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    daily, until = quota_details(
        "quota_parallel_requests", {}, "Fri, 06 Nov 2026 18:01:00 GMT", now
    )
    assert daily is False and until == now + timedelta(minutes=1)
    assert error_type({"code": 429, "errors": [{"error_type": "quota_requests_by_uid"}]}) == (
        "quota_requests_by_uid"
    )
    assert error_type({"code": 429, "errors": None}) == ""
    assert quota_details("", {"errors": None}, "not-a-date", now)[1] == now + timedelta(seconds=305)


def test_sync_persists_request_count_and_specific_daily_error(account, client):
    project = Project.objects.create(name="Quota", domain="quota.example")
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project, connection=account, counter_id="1", counter_name="Quota"
    )

    def opener(*_args, **_kwargs):
        raise limited("quota_requests_by_counter_id")

    api = MetrikaClient(account, opener=opener)
    run = sync_metrika(mapping=mapping, report_month=date(2026, 10, 1), client=api)
    run.refresh_from_db()
    assert run.status == run.Status.FAILED
    assert run.request_count == 1 and run.retry_count == 0
    assert run.quota_error_code == "quota_requests_by_counter_id"
    assert "суточная" in run.error_message and "МСК" in run.error_message
    client.force_login(account.user)
    response = client.get(reverse("yandex:metrika-sync-status", args=[project.pk, run.pk]))
    assert response.status_code == 400
    assert response.json()["request_count"] == 1
    assert response.json()["quota_error_code"] == run.quota_error_code
    assert "test-private-token" not in response.content.decode()


def test_successful_sync_keeps_counters_including_retry_and_restores_callback(account, monkeypatch):
    project = Project.objects.create(name="Quota", domain="quota.example")
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project, connection=account, counter_id="1", counter_name="Quota"
    )
    clock, calls = Clock(), []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        if len(calls) == 3:
            raise limited("quota_parallel_requests", retry_after="5")
        return Response({"data": [], "totals": [0, 0, 0, 0, 0]})

    def fetch_month(client, _mapping, month, **_kwargs):
        for _ in range(2):
            client.stat(ids="1", metrics="ym:s:visits")
        return {
            "period_start": month.isoformat(),
            "period_end": month_end(month).isoformat(),
            "metrics": [],
            "sampled": False,
            "sample_share": 1,
        }

    monkeypatch.setattr("apps.yandex.services._fetch_month", fetch_month)
    api = MetrikaClient(account, opener=opener, sleep=clock.sleep, clock=clock.now)
    run = sync_metrika(mapping=mapping, report_month=date(2026, 10, 1), client=api)
    run.refresh_from_db()
    assert run.status == run.Status.SUCCESS
    assert run.request_count == len(calls) == 9
    assert run.retry_count == 1
    assert run.wait_until is None
    assert api.progress is None


def test_waiting_status_and_old_logs_show_observed_counts_only(account, client):
    project = Project.objects.create(name="Quota", domain="quota.example")
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project, connection=account, counter_id="1", counter_name="Quota"
    )
    run = YandexMetrikaSyncRun.objects.create(
        mapping=mapping,
        report_month=date(2026, 10, 1),
        request_count=201,
        retry_count=1,
        quota_error_code="quota_parallel_requests",
        wait_until=timezone.now() + timedelta(minutes=5),
    )
    client.force_login(account.user)
    result = client.get(reverse("yandex:metrika-sync-status", args=[project.pk, run.pk])).json()
    assert result["queued"] is True and result["request_count"] == 201
    assert "ожидает" in result["message"] and "201" in result["message"]
    assert (
        YandexMetrikaSyncRun.objects.create(
            mapping=mapping, report_month=date(2026, 9, 1)
        ).request_count
        is None
    )


def test_worker_does_not_expire_a_live_waiting_sync(account):
    project = Project.objects.create(name="Quota", domain="quota.example")
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project, connection=account, counter_id="1", counter_name="Quota"
    )
    active = YandexMetrikaSyncRun.objects.create(
        mapping=mapping, report_month=date(2026, 10, 1), heartbeat_at=timezone.now()
    )
    stale = YandexMetrikaSyncRun.objects.create(mapping=mapping, report_month=date(2026, 9, 1))
    YandexMetrikaSyncRun.objects.filter(pk__in=[active.pk, stale.pk]).update(
        started_at=timezone.now() - timedelta(hours=1)
    )
    call_command("run_metrika_sync_worker", once=True)
    active.refresh_from_db()
    stale.refresh_from_db()
    assert active.status == active.Status.RUNNING
    assert stale.status == stale.Status.FAILED

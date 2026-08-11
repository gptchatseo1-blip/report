import io
import json
import urllib.error
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import SourceSnapshot
from apps.projects.models import Project
from apps.yandex.client import WebmasterClient
from apps.yandex.crypto import encrypt_token
from apps.yandex.models import YandexConnection, YandexWebmasterProjectMapping
from apps.yandex.services import sync_webmaster

pytestmark = pytest.mark.django_db
KEY = "rN4-j6VCo2PxKB9RCqaVvzwuR4mqUUqe5xzHIZfyg3A="


@pytest.fixture
def context(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = KEY
    settings.YANDEX_CLIENT_ID = "id"
    settings.YANDEX_CLIENT_SECRET = "secret"
    settings.YANDEX_REDIRECT_URI = "https://example.test/yandex/oauth/callback/"
    settings.YANDEX_MAX_RETRIES = 2
    user = get_user_model().objects.create_user("webmaster", password="password")
    project = Project.objects.create(name="Site", domain="site.example")
    connection = YandexConnection.objects.create(
        user=user,
        access_token_encrypted=encrypt_token("access-token"),
        refresh_token_encrypted=encrypt_token("refresh-token"),
        scopes=["metrika:read", "webmaster:read"],
    )
    return user, project, connection


class FakeWebmaster:
    def user(self):
        return {"user_id": 7}

    def hosts(self, user_id):
        return iter(
            [{"host_id": "https:site.example:443", "ascii_host_url": "https://site.example"}]
        )

    def host(self, user_id, host_id):
        return {"host_id": host_id, "verification": "VERIFIED"}

    def summary(self, user_id, host_id):
        return {
            "searchable_pages_count": 25,
            "excluded_pages_count": 3,
            "problems": [{"type": "FATAL"}],
        }

    def search_query_history(self, *args, **kwargs):
        assert kwargs["device_type_indicator"] == "ALL"
        return {
            "indicators": {
                "TOTAL_SHOWS": [{"date": "2026-01-01", "value": 100}],
                "TOTAL_CLICKS": [{"date": "2026-01-01", "value": 5}],
                "AVG_SHOW_POSITION": [{"date": "2026-01-01", "value": 4}],
            }
        }

    def search_urls_history(self, *args, **kwargs):
        return {"indicators": {"SEARCHABLE": [{"date": "2026-01-31", "value": 20}]}}

    def indexing_history(self, *args, **kwargs):
        return {
            "indicators": {
                "APPEARED_IN_SEARCH": [{"value": 4}],
                "REMOVED_FROM_SEARCH": [{"value": 2}],
            }
        }

    def squ_history(self, *args, **kwargs):
        return {"indicators": {"sqi": [{"date": "2026-01-31", "value": 80}]}}


def mapping(context):
    _, project, connection = context
    return YandexWebmasterProjectMapping.objects.create(
        project=project,
        connection=connection,
        host_id="https:site.example:443",
        host_url="https://site.example",
        verification_status="VERIFIED",
    )


def test_sync_calculates_ctr_periods_and_is_idempotent(context):
    item = mapping(context)
    first = sync_webmaster(
        mapping=item, report_month=date(2026, 3, 1), user=context[0], client=FakeWebmaster()
    )
    assert first.status == first.Status.SUCCESS
    snapshots = SourceSnapshot.objects.filter(source=SourceSnapshot.Source.WEBMASTER).order_by(
        "period_start"
    )
    assert list(snapshots.values_list("period_start", flat=True)) == [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
    ]
    march = snapshots.last()
    points = {point.metric_code: point.numeric_value for point in march.metrics.all()}
    assert points["search_ctr"] == 5
    assert points["searchable_pages_count"] == 25
    assert march.payload["problems"] == [{"type": "FATAL"}]
    ids = list(snapshots.values_list("id", flat=True))
    sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=FakeWebmaster())
    assert (
        list(
            SourceSnapshot.objects.filter(source=SourceSnapshot.Source.WEBMASTER)
            .order_by("period_start")
            .values_list("id", flat=True)
        )
        == ids
    )


def test_zero_impressions_does_not_invent_ctr(context):
    fake = FakeWebmaster()
    fake.search_query_history = lambda *a, **k: {
        "indicators": {"TOTAL_SHOWS": [{"value": 0}], "TOTAL_CLICKS": [{"value": 0}]}
    }
    sync_webmaster(mapping=mapping(context), report_month=date(2026, 3, 1), client=fake)
    codes = SourceSnapshot.objects.get(period_start=date(2026, 3, 1)).metrics.values_list(
        "metric_code", flat=True
    )
    assert "search_ctr" not in codes


def test_all_fetches_happen_before_atomic_write(context):
    item = mapping(context)
    fake = FakeWebmaster()
    fake.squ_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("access-token"))
    run = sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=fake)
    assert run.status == run.Status.FAILED
    assert "access-token" not in run.error_message
    assert not SourceSnapshot.objects.exists()


def test_host_selection_uses_server_list_and_requires_mismatch_confirmation(
    client, context, monkeypatch
):
    user, project, connection = context
    client.force_login(user)
    monkeypatch.setattr(WebmasterClient, "user", lambda self: {"user_id": 7})
    monkeypatch.setattr(
        WebmasterClient,
        "hosts",
        lambda self, uid: iter(
            [
                {
                    "host_id": "allowed",
                    "ascii_host_url": "https://other.example",
                    "verification": "VERIFIED",
                    "main_mirror": "https://www.other.example",
                }
            ]
        ),
    )
    url = reverse("yandex:select-host", args=[project.id])
    client.post(
        url, {"connection_id": connection.id, "host_id": "forged", "confirm_domain_mismatch": "on"}
    )
    assert not YandexWebmasterProjectMapping.objects.exists()
    client.post(url, {"connection_id": connection.id, "host_id": "allowed"})
    assert not YandexWebmasterProjectMapping.objects.exists()
    client.post(
        url, {"connection_id": connection.id, "host_id": "allowed", "confirm_domain_mismatch": "on"}
    )
    saved = project.yandex_webmaster_mapping
    assert saved.host_id == "allowed" and saved.domain_mismatch_confirmed


def test_missing_scope_requires_reauthorization(client, context):
    user, project, connection = context
    connection.scopes = ["metrika:read"]
    connection.save(update_fields=["scopes"])
    client.force_login(user)
    response = client.post(
        reverse("yandex:select-host", args=[project.id]),
        {"connection_id": connection.id, "host_id": "x"},
        follow=True,
    )
    assert "повторная авторизация" in response.content.decode().lower()
    assert not YandexWebmasterProjectMapping.objects.exists()


def test_hosts_pagination_and_retry(context):
    connection = context[2]
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "limited", {"Retry-After": "0"}, io.BytesIO()
            )
        offset = int(request.full_url.split("offset=")[1].split("&")[0])
        return Response({"hosts": [{"host_id": str(offset)}] if offset < 2 else []})

    api = WebmasterClient(connection, opener=opener, sleep=lambda _: None)
    assert [row["host_id"] for row in api.hosts(7, page_size=1)] == ["0", "1"]
    assert len(calls) == 4


def test_mutating_routes_reject_get(client, context):
    user, project, _ = context
    client.force_login(user)
    for name in ("select-host", "sync-webmaster", "oauth-start"):
        assert client.get(reverse(f"yandex:{name}", args=[project.id])).status_code == 405


def test_version_docx_xlsx_never_call_live_webmaster(context, settings, tmp_path, monkeypatch):
    from apps.metrics.synthetic import sync_synthetic_metrics
    from apps.reports.exporting import generate_artifact
    from apps.reports.models import Report
    from apps.reports.services import create_report_version

    settings.MEDIA_ROOT = tmp_path
    project = context[1]
    sync_synthetic_metrics(project=project, report_month=date(2026, 3, 1))
    report = Report.objects.create(project=project, report_month=date(2026, 3, 1))
    version = create_report_version(report=report, created_by=context[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("live Webmaster API called after version fixation")

    monkeypatch.setattr(WebmasterClient, "_request", forbidden)
    for kind in ("docx", "xlsx"):
        artifact = generate_artifact(version=version, artifact_type=kind, is_draft=True)
        assert artifact.status == artifact.Status.READY
        assert artifact.size > 0

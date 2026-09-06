import io
import json
import urllib.error
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import SourceSnapshot
from apps.projects.models import Project
from apps.yandex.client import (
    MetrikaClient,
    WebmasterClient,
    YandexAPIError,
    YandexUnauthorized,
)
from apps.yandex.crypto import encrypt_token
from apps.yandex.models import (
    YandexConnection,
    YandexWebmasterProjectMapping,
    YandexWebmasterSyncRun,
)
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
        scopes=["metrika:read", "webmaster:hostinfo", "webmaster:verify"],
    )
    return user, project, connection


class FakeWebmaster:
    def __init__(self):
        self.user_calls = 0

    def user(self):
        self.user_calls += 1
        return {"user_id": 7}

    def hosts(self, user_id):
        return [
            {
                "host_id": "https:site.example:443",
                "ascii_host_url": "https://site.example",
                "verified": True,
                "main_mirror": {
                    "ascii_host_url": "https://www.site.example",
                    "unicode_host_url": "https://www.site.example",
                },
            }
        ]

    def host(self, user_id, host_id):
        return {"host_id": host_id, "verification": "VERIFIED"}

    def summary(self, user_id, host_id):
        return {
            "searchable_pages_count": 25,
            "excluded_pages_count": 3,
            "site_problems": {"FATAL": 1, "CRITICAL": 2, "POSSIBLE_PROBLEM": 3},
        }

    def search_query_history(self, *args, **kwargs):
        assert kwargs["device_type_indicator"] == "ALL"
        prefix = kwargs["date_from"][:8]
        return {
            "indicators": {
                "TOTAL_SHOWS": [
                    {"date": f"{prefix}10", "value": 100},
                    {"date": f"{prefix}11", "value": 300},
                ],
                "TOTAL_CLICKS": [
                    {"date": f"{prefix}10", "value": 5},
                    {"date": f"{prefix}11", "value": 15},
                ],
                "AVG_SHOW_POSITION": [
                    {"date": f"{prefix}10", "value": 10},
                    {"date": f"{prefix}11", "value": 2},
                    {"date": f"{prefix}12", "value": 99},
                ],
            }
        }

    def search_urls_history(self, *args, **kwargs):
        prefix = kwargs["date_from"][:8]
        return {
            "history": [
                {"date": kwargs["date_to"], "value": 20},
                {"date": f"{prefix}05", "value": 10},
            ]
        }

    def indexing_history(self, *args, **kwargs):
        prefix = kwargs["date_from"][:8]
        return {
            "indicators": {
                "APPEARED_IN_SEARCH": [{"date": f"{prefix}20", "value": 4}],
                "REMOVED_FROM_SEARCH": [{"date": f"{prefix}21", "value": 2}],
            }
        }

    def squ_history(self, *args, **kwargs):
        prefix = kwargs["date_from"][:8]
        return {
            "points": [
                {"date": f"{prefix}25", "value": 80},
                {"date": f"{prefix}02", "value": 60},
            ]
        }


class FakeWebmasterQueryAnalytics(FakeWebmaster):
    def __init__(self):
        super().__init__()
        self.analytics_calls = []

    def query_analytics(self, *args, **kwargs):
        self.analytics_calls.append(kwargs)
        start = kwargs["date_from"]
        return {
            "count": 2,
            "text_indicator_to_statistics": [
                {
                    "text_indicator": {"type": "QUERY", "value": "clinic"},
                    "statistics": [
                        {"date": start, "field": "IMPRESSIONS", "value": 100},
                        {"date": start, "field": "CLICKS", "value": 10},
                        {"date": start, "field": "POSITION", "value": 4},
                    ],
                },
                {
                    "text_indicator": {"type": "QUERY", "value": "doctor"},
                    "statistics": [
                        {"date": start, "field": "IMPRESSIONS", "value": 20},
                        {"date": start, "field": "CLICKS", "value": 2},
                        {"date": start, "field": "POSITION", "value": 9},
                    ],
                },
            ],
        }

    def popular_search_queries(self, *args, **kwargs):
        raise AssertionError("Query Analytics already supplied the popular rows")


def webmaster_http_error(code, error_code):
    return urllib.error.HTTPError(
        "https://api.webmaster.yandex.net/v4/safe",
        code,
        "provider error",
        {},
        io.BytesIO(json.dumps({"error_code": error_code}).encode()),
    )


def test_host_verification_error_is_not_misreported_as_reauthorization(context):
    def opener(*args, **kwargs):
        raise webmaster_http_error(403, "HOST_NOT_VERIFIED")

    client = WebmasterClient(context[2], opener=opener)

    with pytest.raises(YandexAPIError) as raised:
        client.summary("user", "host")

    assert type(raised.value) is YandexAPIError
    assert raised.value.http_status == 403
    assert raised.value.error_code == "HOST_NOT_VERIFIED"


def test_webmaster_permission_error_still_requires_reauthorization(context):
    def opener(*args, **kwargs):
        raise webmaster_http_error(403, "ACCESS_DENIED")

    client = WebmasterClient(context[2], opener=opener)

    with pytest.raises(YandexUnauthorized):
        client.user()


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
    assert points["average_position"] == 4
    assert points["indexed_pages"] == 20
    assert points["iks"] == 80
    assert points["searchable_pages_count"] == 25
    assert march.payload["site_problems"] == {
        "FATAL": 1,
        "CRITICAL": 2,
        "POSSIBLE_PROBLEM": 3,
    }
    assert march.payload["actual_period"] == {
        "date_from": "2026-03-02",
        "date_to": "2026-03-31",
    }
    ids = list(snapshots.values_list("id", flat=True))
    cached_api = FakeWebmaster()
    cached = sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=cached_api)
    assert cached_api.user_calls == 1
    assert (cached.fetched_period_count, cached.reused_period_count) == (1, 2)
    assert (
        list(
            SourceSnapshot.objects.filter(source=SourceSnapshot.Source.WEBMASTER)
            .order_by("period_start")
            .values_list("id", flat=True)
        )
        == ids
    )

    refreshed_api = FakeWebmaster()
    refreshed = sync_webmaster(
        mapping=item,
        report_month=date(2026, 3, 1),
        client=refreshed_api,
        force_refresh=True,
    )
    assert refreshed.status == refreshed.Status.SUCCESS
    assert refreshed_api.user_calls == 1
    assert (refreshed.fetched_period_count, refreshed.reused_period_count) == (3, 0)


def test_current_webmaster_totals_use_same_search_location_as_service(context):
    item = mapping(context)
    api = FakeWebmasterQueryAnalytics()

    run = sync_webmaster(
        mapping=item, report_month=date(2026, 3, 1), user=context[0], client=api
    )

    assert run.status == run.Status.SUCCESS
    assert len(api.analytics_calls) == 2
    assert all(
        call["search_location"] == "ALL_LOCATIONS_ORGANIC" for call in api.analytics_calls
    )
    march = SourceSnapshot.objects.get(period_start=date(2026, 3, 1))
    points = {point.metric_code: point.numeric_value for point in march.metrics.all()}
    assert points["search_impressions"] == 120
    assert points["search_clicks"] == 12
    assert float(points["average_position"]) == pytest.approx(4.8333, abs=0.0001)
    assert march.payload["query_summary"]["shows"] == "120"
    assert [row["query"] for row in march.payload["popular_queries"]] == ["clinic", "doctor"]


def test_zero_impressions_does_not_invent_ctr(context):
    fake = FakeWebmaster()
    fake.search_query_history = lambda *a, **k: {
        "indicators": {
            "TOTAL_SHOWS": [{"date": "2026-03-01", "value": 0}],
            "TOTAL_CLICKS": [{"date": "2026-03-01", "value": 0}],
        }
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


def test_optional_missing_history_keeps_available_webmaster_data(context):
    item = mapping(context)
    fake = FakeWebmaster()
    fake.host = lambda *_: (_ for _ in ()).throw(AssertionError("redundant host request"))
    fake.squ_history = lambda *a, **k: (_ for _ in ()).throw(
        YandexAPIError(
            "safe",
            http_status=404,
            error_code="HOST_NOT_INDEXED",
        )
    )
    fake.summary = lambda *a, **k: (_ for _ in ()).throw(
        YandexAPIError(
            "safe",
            http_status=404,
            error_code="HOST_NOT_INDEXED",
        )
    )

    run = sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=fake)

    assert run.status == run.Status.SUCCESS
    assert SourceSnapshot.objects.count() == 3
    march = SourceSnapshot.objects.get(period_start=date(2026, 3, 1))
    codes = set(march.metrics.values_list("metric_code", flat=True))
    assert {"search_clicks", "search_impressions", "indexed_pages"} <= codes
    assert "iks" not in codes


def test_unverified_host_error_has_safe_actionable_message(context):
    item = mapping(context)
    fake = FakeWebmaster()
    fake.summary = lambda *a, **k: (_ for _ in ()).throw(
        YandexAPIError(
            "provider response that must stay private",
            http_status=404,
            error_code="HOST_NOT_VERIFIED",
        )
    )

    run = sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=fake)

    assert run.status == run.Status.FAILED
    assert run.error_message == "Проверьте подтверждение выбранного сайта в Яндекс.Вебмастере."
    assert "provider response" not in run.error_message
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
        lambda self, uid: [
            {
                "host_id": "allowed",
                "ascii_host_url": "https://other.example",
                "verified": True,
                "main_mirror": {
                    "ascii_host_url": "https://www.other.example",
                    "unicode_host_url": "https://другой.example",
                },
            }
        ],
    )
    url = reverse("yandex:select-host", args=[project.id])
    client.post(
        url, {"connection_id": connection.id, "host_id": "forged", "confirm_domain_mismatch": "on"}
    )
    assert not YandexWebmasterProjectMapping.objects.exists()
    client.post(url, {"connection_id": connection.id, "host_id": "allowed"})
    assert not YandexWebmasterProjectMapping.objects.exists()
    response = client.post(
        url,
        {"connection_id": connection.id, "host_id": "allowed", "confirm_domain_mismatch": "on"},
        follow=True,
    )
    saved = project.yandex_webmaster_mapping
    assert saved.host_id == "allowed" and saved.domain_mismatch_confirmed
    assert saved.verification_status == "VERIFIED"
    assert saved.main_mirror == "https://www.other.example"
    assert "Сайт Яндекс.Вебмастера сохранён." in response.content.decode()


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


def test_hosts_make_one_request_without_query_parameters(context):
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
        return Response({"hosts": [{"host_id": "first"}, {"host_id": "second"}]})

    api = WebmasterClient(connection, opener=opener, sleep=lambda _: None)
    assert [row["host_id"] for row in api.hosts(7)] == ["first", "second"]
    assert calls == ["https://api.webmaster.yandex.net/v4/user/7/hosts"]


def test_query_analytics_posts_ui_location_and_period(context):
    connection = context[2]
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"count": 1, "text_indicator_to_statistics": [{"statistics": []}]}
            ).encode()

    def opener(request, timeout):
        requests.append(request)
        return Response()

    api = WebmasterClient(connection, opener=opener, sleep=lambda _: None)
    result = api.query_analytics(
        7,
        "https:site.example:443",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )

    assert result["count"] == 1
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.data)
    assert request.method == "POST"
    assert request.headers["Content-type"] == "application/json; charset=UTF-8"
    assert body["search_location"] == "ALL_LOCATIONS_ORGANIC"
    assert body["filters"]["statistic_filters"][0]["from"] == "2026-08-01"
    assert body["filters"]["statistic_filters"][0]["to"] == "2026-08-31"


def test_mutating_routes_reject_get(client, context):
    user, project, _ = context
    client.force_login(user)
    for name in ("select-host", "sync-webmaster", "oauth-start"):
        assert client.get(reverse(f"yandex:{name}", args=[project.id])).status_code == 405


def test_webmaster_run_can_only_be_deleted_by_post_and_keeps_snapshots(client, context):
    user, project, _ = context
    item = mapping(context)
    run = YandexWebmasterSyncRun.objects.create(
        mapping=item,
        report_month=date(2026, 3, 1),
        status=YandexWebmasterSyncRun.Status.FAILED,
    )
    snapshot = SourceSnapshot.objects.create(
        project=project,
        source=SourceSnapshot.Source.WEBMASTER,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        checksum="kept",
        payload={},
    )
    url = reverse("yandex:delete-webmaster-run", args=[project.id, run.id])
    client.force_login(user)

    assert client.get(url).status_code == 405
    response = client.post(url)

    assert response.status_code == 302
    assert not YandexWebmasterSyncRun.objects.filter(pk=run.id).exists()
    assert SourceSnapshot.objects.filter(pk=snapshot.pk).exists()


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


def test_unverified_host_is_visible_but_cannot_be_selected(client, context, monkeypatch):
    user, project, connection = context
    client.force_login(user)
    host = {
        "host_id": "unverified",
        "ascii_host_url": "https://site.example",
        "verified": False,
        "main_mirror": None,
    }
    monkeypatch.setattr(WebmasterClient, "user", lambda self: {"user_id": 7})
    monkeypatch.setattr(WebmasterClient, "hosts", lambda self, uid: [host])
    monkeypatch.setattr(WebmasterClient, "counters", lambda self: iter([]))
    response = client.get(reverse("yandex:connection", args=[project.id]))
    assert "unverified" in response.content.decode()
    response = client.post(
        reverse("yandex:select-host", args=[project.id]),
        {"connection_id": connection.id, "host_id": "unverified"},
        follow=True,
    )
    assert "не подтверждён" in response.content.decode()
    assert not YandexWebmasterProjectMapping.objects.exists()


def test_connection_uses_one_compact_webmaster_select(client, context, monkeypatch):
    user, project, _ = context
    client.force_login(user)
    monkeypatch.setattr(MetrikaClient, "counters", lambda *_: iter([]))
    monkeypatch.setattr(WebmasterClient, "user", lambda self: {"user_id": 7})
    monkeypatch.setattr(
        WebmasterClient,
        "hosts",
        lambda self, uid: [
            {
                "host_id": "matching",
                "ascii_host_url": "https://site.example",
                "verified": True,
            },
            {
                "host_id": "other",
                "ascii_host_url": "https://other.example",
                "verified": True,
            },
            {
                "host_id": "unverified",
                "ascii_host_url": "https://unverified.example",
                "verified": False,
            },
        ],
    )

    html = client.get(reverse("yandex:connection", args=[project.id])).content.decode()

    assert "3. Яндекс.Вебмастер" in html
    assert html.count(reverse("yandex:select-host", args=[project.id])) == 1
    assert '<select id="host-id"' in html
    assert 'value="matching" data-domain-mismatch="false"' in html
    assert 'value="other" data-domain-mismatch="true"' in html
    assert 'value="unverified" data-domain-mismatch="true" disabled' in html


def test_webmaster_page_has_compact_sync_help_and_delete_actions(client, context, monkeypatch):
    user, project, _ = context
    item = mapping(context)
    run = YandexWebmasterSyncRun.objects.create(
        mapping=item,
        report_month=date(2026, 3, 1),
        status=YandexWebmasterSyncRun.Status.FAILED,
        error_message="Безопасная ошибка",
    )
    client.force_login(user)
    monkeypatch.setattr(MetrikaClient, "counters", lambda *_: iter([]))
    monkeypatch.setattr(WebmasterClient, "user", lambda self: {"user_id": 7})
    monkeypatch.setattr(WebmasterClient, "hosts", lambda self, uid: FakeWebmaster().hosts(uid))

    html = client.get(reverse("yandex:connection", args=[project.id])).content.decode()

    assert "3. Яндекс.Вебмастер" in html
    assert "Сервис загрузит этот месяц и два предыдущих" in html
    assert 'class="sync-form"' in html
    assert reverse("yandex:delete-webmaster-run", args=[project.id, run.id]) in html


def test_actual_period_uses_only_received_dates_and_empty_has_reason(context):
    item = mapping(context)
    fake = FakeWebmaster()
    fake.search_query_history = lambda *a, **k: {
        "indicators": {
            "TOTAL_SHOWS": [{"date": "2026-03-14", "value": 10}],
            "TOTAL_CLICKS": [{"date": "2026-03-14", "value": 1}],
            "AVG_SHOW_POSITION": [{"date": "2026-03-14", "value": 7}],
        }
    }
    fake.search_urls_history = lambda *a, **k: {"history": []}
    fake.indexing_history = lambda *a, **k: {"indicators": {}}
    fake.squ_history = lambda *a, **k: {"points": []}
    sync_webmaster(mapping=item, report_month=date(2026, 3, 1), client=fake)
    snapshot = SourceSnapshot.objects.get(period_start=date(2026, 3, 1))
    assert snapshot.payload["actual_period"] == {
        "date_from": "2026-03-14",
        "date_to": "2026-03-14",
    }
    assert snapshot.payload["availability_reason"] is None

    empty = FakeWebmaster()
    empty.search_query_history = lambda *a, **k: {"indicators": {}}
    empty.search_urls_history = lambda *a, **k: {"history": []}
    empty.indexing_history = lambda *a, **k: {"indicators": {}}
    empty.squ_history = lambda *a, **k: {"points": []}
    sync_webmaster(mapping=item, report_month=date(2026, 4, 1), client=empty)
    april = SourceSnapshot.objects.get(period_start=date(2026, 4, 1))
    assert april.payload["actual_period"] is None
    assert april.payload["availability_reason"] == "API не вернул данные за период."
    codes = set(april.metrics.values_list("metric_code", flat=True))
    assert codes == {"searchable_pages_count", "excluded_pages_count"}

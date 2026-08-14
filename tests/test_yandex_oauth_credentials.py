import urllib.parse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.projects.models import Project
from apps.reports.models import Report
from apps.yandex.crypto import decrypt_token, encrypt_token
from apps.yandex.models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexOAuthCredential,
    YandexWebmasterProjectMapping,
)

pytestmark = pytest.mark.django_db
FERNET_KEY = "rN4-j6VCo2PxKB9RCqaVvzwuR4mqUUqe5xzHIZfyg3A="
CALLBACK = "https://report.example/yandex/oauth/callback/"


@pytest.fixture
def oauth_context(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    settings.YANDEX_CLIENT_ID = ""
    settings.YANDEX_CLIENT_SECRET = ""
    settings.YANDEX_REDIRECT_URI = ""
    staff = get_user_model().objects.create_user("oauth-admin", password="password", is_staff=True)
    user = get_user_model().objects.create_user("oauth-user", password="password")
    project = Project.objects.create(name="OAuth project", domain="oauth.example")
    return staff, user, project


def save_credentials(client, *, client_id="client-id", secret="client-secret", callback=CALLBACK):
    return client.post(
        reverse("yandex:oauth-credentials"),
        {"client_id": client_id, "client_secret": secret, "redirect_uri": callback},
    )


def test_staff_stores_one_encrypted_credential_without_rendering_secret(client, oauth_context):
    staff, _, project = oauth_context
    client.force_login(staff)
    response = save_credentials(client)
    assert response.status_code == 302

    credential = YandexOAuthCredential.objects.get(pk=1)
    assert credential.client_id == "client-id"
    assert credential.redirect_uri == CALLBACK
    assert b"client-secret" not in bytes(credential.client_secret_encrypted)
    assert decrypt_token(credential.client_secret_encrypted) == "client-secret"

    body = client.get(reverse("yandex:oauth-credentials")).content.decode()
    assert "client-secret" not in body
    assert "••••cret" in body
    project_list = client.get(reverse("reports:projects")).content.decode()
    assert reverse("yandex:oauth-credentials") in project_list
    assert reverse("topvisor:credentials") in project_list
    assert reverse("topvisor:connection", args=[project.id]) in project_list
    assert reverse("yandex:connection", args=[project.id]) in project_list


def test_credentials_page_requires_staff_and_csrf(oauth_context):
    staff, user, _ = oauth_context
    url = reverse("yandex:oauth-credentials")
    client = Client()
    client.force_login(user)
    assert client.get(url).status_code == 302
    assert client.post(url, {}).status_code == 302
    assert not YandexOAuthCredential.objects.exists()

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(staff)
    assert csrf_client.post(url, {}).status_code == 403


def test_blank_secret_is_preserved_and_client_change_requires_reauthorization(
    client, oauth_context
):
    staff, _, project = oauth_context
    client.force_login(staff)
    save_credentials(client)
    credential = YandexOAuthCredential.objects.get()
    encrypted_secret = bytes(credential.client_secret_encrypted)
    connection = YandexConnection.objects.create(
        user=staff,
        access_token_encrypted=encrypt_token("access-token"),
        refresh_token_encrypted=encrypt_token("refresh-token"),
        scopes=["metrika:read", "webmaster:hostinfo"],
    )
    mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="42",
        counter_name="Counter",
        counter_domain=project.domain,
    )
    report = Report.objects.create(project=project, report_month="2026-07-01")

    response = save_credentials(client, client_id="replacement-id", secret="")
    assert response.status_code == 302
    credential.refresh_from_db()
    connection.refresh_from_db()
    assert bytes(credential.client_secret_encrypted) == encrypted_secret
    assert credential.client_id == "replacement-id"
    assert connection.active is False
    assert bytes(connection.access_token_encrypted) == b""
    assert connection.refresh_token_encrypted is None
    assert YandexMetrikaProjectMapping.objects.filter(pk=mapping.pk).exists()
    assert Report.objects.filter(pk=report.pk).exists()


def test_database_credentials_override_env_and_oauth_uses_real_webmaster_scope(
    client, oauth_context, settings
):
    staff, user, project = oauth_context
    settings.YANDEX_CLIENT_ID = "legacy-id"
    settings.YANDEX_CLIENT_SECRET = "legacy-secret"
    settings.YANDEX_REDIRECT_URI = "https://legacy.example/yandex/oauth/callback/"
    client.force_login(staff)
    save_credentials(client, client_id="database-id", secret="database-secret")

    client.force_login(user)
    response = client.post(reverse("yandex:oauth-start", args=[project.id]))
    assert response.status_code == 302
    parsed = urllib.parse.urlsplit(response.url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["client_id"] == ["database-id"]
    assert query["redirect_uri"] == [CALLBACK]
    assert query["scope"] == ["metrika:read webmaster:hostinfo"]
    assert "webmaster:read" not in response.url


def test_active_connection_shows_safe_reauthorization_action(client, oauth_context, monkeypatch):
    staff, user, project = oauth_context
    client.force_login(staff)
    save_credentials(client)
    connection = YandexConnection.objects.create(
        user=user,
        access_token_encrypted=encrypt_token("access-token"),
        refresh_token_encrypted=encrypt_token("refresh-token"),
        scopes=["metrika:read", "webmaster:hostinfo"],
    )
    monkeypatch.setattr("apps.yandex.views.MetrikaClient.counters", lambda self: [])
    monkeypatch.setattr(
        "apps.yandex.views.WebmasterClient.user", lambda self: {"user_id": "user-id"}
    )
    monkeypatch.setattr("apps.yandex.views.WebmasterClient.hosts", lambda self, user_id: [])

    client.force_login(user)
    response = client.get(reverse("yandex:connection", args=[project.id]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Повторно авторизовать аккаунт Яндекса" in body
    assert "если сервис сообщает об истёкшем доступе или недостающих правах" in body
    assert "Выбранные счётчик, цели и сайт сохранятся" in body
    assert reverse("yandex:oauth-start", args=[project.id]) in body
    assert reverse("yandex:disconnect", args=[connection.id]) in body


def test_reauthorization_reuses_connection_and_preserves_project_selections(
    client, oauth_context, monkeypatch
):
    staff, user, project = oauth_context
    client.force_login(staff)
    save_credentials(client)
    connection = YandexConnection.objects.create(
        user=user,
        account_login="old@yandex.ru",
        access_token_encrypted=encrypt_token("old-access"),
        refresh_token_encrypted=encrypt_token("old-refresh"),
        scopes=["metrika:read"],
        active=False,
    )
    metrika_mapping = YandexMetrikaProjectMapping.objects.create(
        project=project,
        connection=connection,
        counter_id="42",
        counter_name="Counter",
        counter_domain=project.domain,
        selected_goals=[{"id": "7", "name": "Goal"}],
    )
    webmaster_mapping = YandexWebmasterProjectMapping.objects.create(
        project=project,
        connection=connection,
        host_id="https:oauth.example:443",
        host_url="https://oauth.example/",
        verification_status="VERIFIED",
    )
    client.force_login(user)
    start = client.post(reverse("yandex:oauth-start", args=[project.id]))
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start.url).query)["state"][0]
    monkeypatch.setattr(
        "apps.yandex.views.exchange_token",
        lambda parameters, credentials=None: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "scope": "metrika:read webmaster:hostinfo",
            "uid": "123",
            "login": "new@yandex.ru",
        },
    )

    response = client.get(
        reverse("yandex:oauth-callback"), {"state": state, "code": "authorization-code"}
    )

    assert response.status_code == 302
    connection.refresh_from_db()
    metrika_mapping.refresh_from_db()
    webmaster_mapping.refresh_from_db()
    assert YandexConnection.objects.filter(user=user).count() == 1
    assert connection.active is True
    assert connection.account_login == "new@yandex.ru"
    assert decrypt_token(connection.access_token_encrypted) == "new-access"
    assert decrypt_token(connection.refresh_token_encrypted) == "new-refresh"
    assert connection.scopes == ["metrika:read", "webmaster:hostinfo"]
    assert metrika_mapping.connection_id == connection.id
    assert metrika_mapping.counter_id == "42"
    assert metrika_mapping.selected_goals == [{"id": "7", "name": "Goal"}]
    assert webmaster_mapping.connection_id == connection.id
    assert webmaster_mapping.host_url == "https://oauth.example/"


def test_invalid_callback_path_does_not_save_or_echo_secret(client, oauth_context):
    staff, _, _ = oauth_context
    client.force_login(staff)
    response = save_credentials(
        client,
        secret="must-not-render",
        callback="https://report.example/wrong/callback/",
    )
    assert response.status_code == 200
    assert "must-not-render" not in response.content.decode()
    assert not YandexOAuthCredential.objects.exists()

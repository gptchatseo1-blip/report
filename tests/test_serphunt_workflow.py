from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.models import Report
from apps.reports.services import create_report_version
from apps.reports.validation import _contains_secret
from apps.serphunt.models import SerphuntCredential, SerphuntProjectMapping
from apps.serphunt.services import sync_positions

pytestmark = pytest.mark.django_db
FERNET_KEY = "rN4-j6VCo2PxKB9RCqaVvzwuR4mqUUqe5xzHIZfyg3A="


def test_secret_validator_checks_values_not_safe_field_names():
    assert not _contains_secret({"api_key_last_four": "1234", "Authorization": "disabled"})
    assert _contains_secret({"request": "Authorization: Bearer secret-token-value"})


def test_project_screen_has_one_settings_label_and_serphunt_button(client):
    staff = get_user_model().objects.create_user("staff", password="secret", is_staff=True)
    Project.objects.create(name="Demo", domain="demo.example")
    client.force_login(staff)

    body = client.get(reverse("reports:projects")).content.decode()

    assert body.count("Настройки:") == 1
    assert reverse("topvisor:credentials") in body
    assert reverse("serphunt:credentials") in body
    assert ">Topvisor<" in body and ">Serphunt<" in body and ">Яндекс<" in body
    assert "Настройки Topvisor" not in body
    assert "Настройки Яндекса" not in body


def test_quick_create_selects_serphunt_without_changing_existing_projects(client):
    user = get_user_model().objects.create_user("creator", password="secret")
    existing = Project.objects.create(name="Existing", domain="existing.example")
    client.force_login(user)

    response = client.post(
        reverse("reports:project-create"),
        {
            "name": "Serphunt project",
            "domain": "serphunt.example",
            "position_provider": Project.PositionProvider.SERPHUNT,
            "timezone": "Europe/Moscow",
            "language": "ru",
        },
    )

    assert response.status_code == 302
    assert Project.objects.get(name="Serphunt project").position_provider == "serphunt"
    existing.refresh_from_db()
    assert existing.position_provider == "topvisor"


def test_serphunt_credentials_are_encrypted_and_not_rendered(client, settings, monkeypatch):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    staff = get_user_model().objects.create_user("serphunt-admin", password="secret", is_staff=True)
    client.force_login(staff)
    monkeypatch.setattr("apps.serphunt.client.SerphuntClient.balance", lambda self: {"balance": 42})

    response = client.post(reverse("serphunt:credentials"), {"api_key": "api-secret-value"})

    assert response.status_code == 302
    credential = SerphuntCredential.objects.get(pk=1)
    assert b"api-secret-value" not in bytes(credential.api_key_encrypted)
    assert credential.get_api_key() == "api-secret-value"
    assert "api-secret-value" not in client.get(reverse("serphunt:credentials")).content.decode()


def test_serphunt_sync_normalizes_positions(settings, monkeypatch):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    project = Project.objects.create(
        name="Positions", domain="positions.example", position_provider="serphunt"
    )
    credential = SerphuntCredential()
    credential.set_api_key("api-secret-value")
    credential.save()
    mapping = SerphuntProjectMapping.objects.create(
        project=project,
        keywords="ключевой запрос",
        search_engines=["yandex"],
        region_id=225,
        region_name="Россия",
    )

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "api-secret-value"

        def start_positions(self, selected_mapping):
            assert selected_mapping == mapping
            return {"task_id": "task-1"}

        def result(self, task_id):
            assert task_id == "task-1"
            return {
                "result": {
                    "225_desktop_ru": {
                        "ключевой запрос": {
                            "yandex": {
                                "https://positions.example/page/": {
                                    "position": 4,
                                    "relevance_url": "https://positions.example/page/",
                                }
                            }
                        }
                    }
                }
            }

    monkeypatch.setattr("apps.serphunt.services.SerphuntClient", FakeClient)

    run = sync_positions(mapping)

    assert run.status == run.Status.SUCCESS
    snapshot = RankingSnapshot.objects.get(project=project)
    assert snapshot.depth_source == RankingSnapshot.DepthSource.SERPHUNT_API
    assert snapshot.search_engine == "yandex" and snapshot.tracked_keyword_count == 1
    position = KeywordPosition.objects.get(ranking_snapshot=snapshot)
    assert position.position_value == 4
    assert position.target_url == "https://positions.example/page/"


def test_project_delete_cascades_only_selected_project(client):
    user = get_user_model().objects.create_user("deleter", password="secret")
    selected = Project.objects.create(name="Delete", domain="delete.example")
    untouched = Project.objects.create(name="Keep", domain="keep.example")
    report = Report.objects.create(project=selected, report_month=date(2026, 8, 1))
    create_report_version(report=report, created_by=user)
    client.force_login(user)

    response = client.post(reverse("reports:project-delete", args=[selected.id]))

    assert response.status_code == 302
    assert not Project.objects.filter(pk=selected.pk).exists()
    assert Project.objects.filter(pk=untouched.pk).exists()

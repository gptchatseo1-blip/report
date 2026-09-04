import json
from datetime import date

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db.models.deletion import RestrictedError
from django.urls import reverse

from apps.imports.models import ImportBatch
from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.models import ProjectReportSettings, Report
from apps.reports.services import create_report_version
from apps.reports.validation import _contains_secret
from apps.serphunt.client import SerphuntClient
from apps.serphunt.models import SerphuntCredential, SerphuntProjectMapping
from apps.serphunt.services import sync_positions
from apps.worklog.models import WorkCategory, WorkLogItem

pytestmark = pytest.mark.django_db
FERNET_KEY = Fernet.generate_key().decode()
TEST_API_KEY = "-".join(("unit", "test", "credential"))


def test_serphunt_client_uses_documented_endpoint_and_bearer_header(settings, monkeypatch):
    settings.SERPHUNT_API_BASE_URL = "https://serphunt.ru/api/v1"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"balance": 42}'

    def fake_open(request, timeout):
        assert request.full_url == "https://serphunt.ru/api/v1/get/billing/balance"
        assert request.get_header("Authorization") == f"Bearer {TEST_API_KEY}"
        assert request.get_header("Content-type") == "application/json; charset=UTF-8"
        assert timeout == settings.SERPHUNT_REQUEST_TIMEOUT_SECONDS
        return Response()

    monkeypatch.setattr("apps.serphunt.client.urlopen", fake_open)

    assert SerphuntClient(f"Bearer {TEST_API_KEY}").balance()["balance"] == 42


def test_secret_validator_checks_values_not_safe_field_names():
    assert not _contains_secret({"api_key_last_four": "1234", "Authorization": "disabled"})
    assert not _contains_secret(["api", "key", "Описание API key без значения"])
    assert _contains_secret({"api_key": TEST_API_KEY})
    assert _contains_secret({"request": f"Authorization: Bearer {TEST_API_KEY}"})


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
    assert ">Параметры<" in body and "Параметры проекта" not in body
    assert 'class="project-delete-button"' in body
    assert 'title="Удалить проект"' in body
    assert 'class="cards project-cards"' in body


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


def test_manual_dynamics_editor_has_every_segment_and_autosaves(client):
    user = get_user_model().objects.create_user("editor", password="secret")
    project = Project.objects.create(
        name="Editor", domain="editor.example", position_provider=Project.PositionProvider.SERPHUNT
    )
    SerphuntProjectMapping.objects.create(
        project=project,
        keywords="запрос",
        search_engines=["yandex", "google"],
        region_id=213,
        region_name="Москва",
    )
    client.force_login(user)

    page = client.get(reverse("reports:report-list", args=[project.id]))
    assert page.status_code == 200
    assert {item["engine"] for item in page.context["topvisor_editor_segments"]} == {
        "yandex",
        "google",
    }
    assert "data-topvisor-manual-segments" in page.content.decode()

    rows = [
        {
            "engine": "yandex",
            "region": "Москва",
            "month": "2026-08-01",
            "top3": 22,
            "top10": 124,
            "top11_30": 84,
            "top3_percent": 7,
            "top10_percent": 34,
            "top11_30_percent": 23,
        }
    ]
    response = client.post(
        reverse("reports:report-settings-save", args=[project.id]),
        data=json.dumps({"topvisor_manual_rows": json.dumps(rows)}),
        content_type="application/json",
    )
    assert response.status_code == 200
    saved = json.loads(
        ProjectReportSettings.objects.get(project=project).values["topvisor_manual_rows"]
    )
    assert saved[0]["top3_percent"] == 7 and saved[0]["top3"] == 22


def test_report_header_handles_long_title_and_has_no_back_button(client):
    user = get_user_model().objects.create_user("header", password="secret")
    project = Project.objects.create(name="Очень длинное название проекта " * 5, domain="long.test")
    client.force_login(user)

    body = client.get(reverse("reports:report-list", args=[project.id])).content.decode()

    assert 'class="report-list-head"' in body
    assert 'class="report-provider-row"' in body
    assert "Провайдер позиций:" in body
    assert ">К проектам<" not in body
    assert "Сравнение с предыдущим месяцем" in body
    assert "По умолчанию данные за квартал." in body


def test_serphunt_credentials_are_encrypted_and_not_rendered(client, settings, monkeypatch):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    staff = get_user_model().objects.create_user("serphunt-admin", password="secret", is_staff=True)
    client.force_login(staff)
    monkeypatch.setattr("apps.serphunt.client.SerphuntClient.balance", lambda self: {"balance": 42})

    response = client.post(reverse("serphunt:credentials"), {"api_key": TEST_API_KEY})

    assert response.status_code == 302
    credential = SerphuntCredential.objects.get(pk=1)
    assert TEST_API_KEY.encode() not in bytes(credential.api_key_encrypted)
    assert credential.get_api_key() == TEST_API_KEY
    assert TEST_API_KEY not in client.get(reverse("serphunt:credentials")).content.decode()


def test_serphunt_sync_normalizes_positions(settings, monkeypatch):
    settings.CREDENTIAL_ENCRYPTION_KEY = FERNET_KEY
    project = Project.objects.create(
        name="Positions", domain="positions.example", position_provider="serphunt"
    )
    credential = SerphuntCredential()
    credential.set_api_key(TEST_API_KEY)
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
            assert api_key == TEST_API_KEY

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
    batch = ImportBatch.objects.create(
        project=selected,
        original_filename="positions.csv",
        source_file="imports/positions.csv",
        file_checksum="a" * 64,
        status=ImportBatch.Status.IMPORTED,
        snapshot_date=date(2026, 8, 31),
        search_engine=ImportBatch.SearchEngine.YANDEX,
        region="Москва",
    )
    RankingSnapshot.objects.create(
        project=selected,
        import_batch=batch,
        snapshot_date=batch.snapshot_date,
        search_engine=batch.search_engine,
        region=batch.region,
    )
    category = WorkCategory.objects.create(project=selected, name="Контент")
    WorkLogItem.objects.create(
        project=selected,
        work_date=date(2026, 8, 15),
        category=category,
        title="Подготовлен материал",
    )
    with pytest.raises(RestrictedError):
        batch.delete()
    with pytest.raises(RestrictedError):
        category.delete()
    client.force_login(user)

    response = client.post(reverse("reports:project-delete", args=[selected.id]))

    assert response.status_code == 302
    assert not Project.objects.filter(pk=selected.pk).exists()
    assert Project.objects.filter(pk=untouched.pk).exists()
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()
    assert not WorkCategory.objects.filter(pk=category.pk).exists()

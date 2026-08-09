import io
import urllib.error
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from docx import Document
from openpyxl import load_workbook

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import Report
from apps.reports.services import create_report_version
from apps.topvisor.client import TopvisorClient, TopvisorCredentials, TopvisorError
from apps.topvisor.models import TopvisorProjectMapping
from apps.topvisor.services import sync_positions

pytestmark = pytest.mark.django_db


class FakeClient:
    def __init__(self, missing_frequency=False):
        self.calls = []
        self.missing_frequency = missing_frequency

    def get_positions(self, project_id, **filters):
        self.calls.append((project_id, filters))
        row = {"query": "купить слона", "position": 7, "frequency": 120}
        if self.missing_frequency:
            row.pop("frequency")
        return iter([row])


class FailingLaterClient(FakeClient):
    def __init__(self, *, fail_at, secret=""):
        super().__init__()
        self.fail_at = fail_at
        self.secret = secret

    def get_positions(self, project_id, **filters):
        self.calls.append((project_id, filters))
        if len(self.calls) == self.fail_at:
            raise TopvisorError(f"Ошибка провайдера {self.secret}")
        return iter([{"query": "купить слона", "position": 7, "frequency": 120}])


def mapping(project, depth=20, engine="google", region="Москва"):
    return TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        topvisor_project_name="Safe project",
        selected_configurations=[
            {
                "id": "7",
                "search_engine": engine,
                "region_name": region,
                "depth": depth,
                "device": "desktop",
            }
        ],
    )


def test_missing_credentials_has_clear_state(client, settings):
    settings.TOPVISOR_USER_ID = settings.TOPVISOR_API_KEY = ""
    user = get_user_model().objects.create_user("user", password="password")
    project = Project.objects.create(name="Site", domain="site.example")
    client.force_login(user)
    response = client.get(reverse("topvisor:connection", args=[project.id]))
    assert response.status_code == 200
    assert "Не настроено" in response.content.decode()
    assert "password" not in response.content.decode()


def test_connection_and_project_configuration_selection(client, settings, monkeypatch):
    settings.TOPVISOR_USER_ID, settings.TOPVISOR_API_KEY = "uid", "super-secret"
    user = get_user_model().objects.create_user("user", password="password")
    project = Project.objects.create(name="Site", domain="connect.example")
    client.force_login(user)
    monkeypatch.setattr(
        TopvisorClient, "iter_projects", lambda self: iter([{"id": 42, "name": "SEO"}])
    )
    monkeypatch.setattr(
        TopvisorClient,
        "get_search_configurations",
        lambda self, _id: [
            {"id": 7, "search_engine": "google", "region_name": "Москва", "depth": 50}
        ],
    )
    response = client.post(
        reverse("topvisor:connection", args=[project.id]),
        {"topvisor_project": "42", "configurations": ["7"]},
    )
    assert response.status_code == 302
    saved = project.topvisor_mapping
    assert saved.topvisor_project_id == "42"
    assert "super-secret" not in repr(saved.selected_configurations)


def test_sync_is_idempotent_requires_frequency_and_keeps_depth_per_segment():
    project = Project.objects.create(name="Site", domain="sync.example")
    selected = mapping(project, depth=20)
    selected.selected_configurations.append(
        {"id": "8", "search_engine": "yandex", "region_name": "Россия", "depth": 100}
    )
    selected.save()
    fake = FakeClient()
    first = sync_positions(mapping=selected, report_month=date(2026, 7, 1), client=fake)
    second = sync_positions(mapping=selected, report_month=date(2026, 7, 1), client=fake)
    assert first.status == first.Status.SUCCESS
    assert len(fake.calls) == 12
    assert RankingSnapshot.objects.count() == 6
    assert KeywordPosition.objects.count() == 6
    assert {(x["search_engine"], x["region"], x["depth"]) for x in second.segments} == {
        ("google", "Москва", 20),
        ("yandex", "Россия", 100),
    }
    assert all(
        item.response_checksum and item.retrieved_at and item.provenance
        for item in RankingSnapshot.objects.all()
    )
    failed = sync_positions(
        mapping=selected, report_month=date(2026, 8, 1), client=FakeClient(True)
    )
    assert failed.status == failed.Status.FAILED
    assert "частотности" in failed.error_message


def test_late_api_error_leaves_no_partial_snapshots_and_records_failed_run():
    project = Project.objects.create(name="Atomic", domain="atomic.example")
    selected = mapping(project)
    run = sync_positions(
        mapping=selected,
        report_month=date(2026, 7, 1),
        client=FailingLaterClient(fail_at=3),
    )
    assert run.status == run.Status.FAILED
    assert run.completed_at is not None
    assert RankingSnapshot.objects.filter(project=project).count() == 0


def test_late_api_error_does_not_change_existing_snapshots():
    project = Project.objects.create(name="Existing", domain="existing.example")
    selected = mapping(project)
    successful = sync_positions(
        mapping=selected, report_month=date(2026, 7, 1), client=FakeClient()
    )
    assert successful.status == successful.Status.SUCCESS
    before = list(
        RankingSnapshot.objects.filter(project=project)
        .order_by("snapshot_date")
        .values_list("id", "response_checksum", "retrieved_at")
    )
    failed = sync_positions(
        mapping=selected,
        report_month=date(2026, 7, 1),
        client=FailingLaterClient(fail_at=3),
    )
    after = list(
        RankingSnapshot.objects.filter(project=project)
        .order_by("snapshot_date")
        .values_list("id", "response_checksum", "retrieved_at")
    )
    assert failed.status == failed.Status.FAILED
    assert after == before


def test_sync_error_redacts_client_credentials():
    project = Project.objects.create(name="Secret", domain="secret.example")
    selected = mapping(project)
    fake = FailingLaterClient(fail_at=2, secret="api-secret")
    fake.credentials = TopvisorCredentials("user-secret", "api-secret")
    run = sync_positions(mapping=selected, report_month=date(2026, 7, 1), client=fake)
    assert run.status == run.Status.FAILED
    assert "api-secret" not in run.error_message
    assert "[скрыто]" in run.error_message


def test_duplicate_normalized_engine_region_pair_is_rejected(client, settings, monkeypatch):
    settings.TOPVISOR_USER_ID, settings.TOPVISOR_API_KEY = "uid", "key"
    user = get_user_model().objects.create_user("duplicate", password="password")
    project = Project.objects.create(name="Duplicate", domain="duplicate.example")
    client.force_login(user)
    configurations = [
        {
            "id": 7,
            "search_engine": "Google",
            "region_name": "Москва",
            "depth": 50,
            "device": "desktop",
        },
        {
            "id": 8,
            "search_engine": " google ",
            "region_name": "  МОСКВА ",
            "depth": 50,
            "device": "mobile",
        },
    ]
    monkeypatch.setattr(
        TopvisorClient, "iter_projects", lambda self: iter([{"id": 42, "name": "SEO"}])
    )
    monkeypatch.setattr(
        TopvisorClient, "get_search_configurations", lambda self, _id: configurations
    )
    response = client.post(
        reverse("topvisor:connection", args=[project.id]),
        {"topvisor_project": "42", "configurations": ["7", "8"]},
    )
    assert response.status_code == 200
    assert "устройство не является измерением" in response.content.decode()
    assert not TopvisorProjectMapping.objects.filter(project=project).exists()


def test_sync_version_and_docx_xlsx_export_do_not_need_live_api(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(name="Site", domain="export-sync.example")
    sync_positions(mapping=mapping(project), report_month=date(2026, 7, 1), client=FakeClient())
    version = create_report_version(
        report=Report.objects.create(project=project, report_month=date(2026, 7, 1))
    )
    checksum = version.snapshot.checksum
    docx = generate_artifact(version=version, artifact_type="docx", is_draft=True)
    xlsx = generate_artifact(version=version, artifact_type="xlsx", is_draft=True)
    with docx.file.open("rb") as stream:
        assert Document(io.BytesIO(stream.read())).paragraphs
    with xlsx.file.open("rb") as stream:
        assert "Позиции" in load_workbook(io.BytesIO(stream.read())).sheetnames
    version.snapshot.refresh_from_db()
    assert version.snapshot.checksum == checksum


def test_client_pagination_and_429_retry(monkeypatch):
    responses = [
        urllib.error.HTTPError("url", 429, "rate", {"Retry-After": "0"}, None),
        {"result": {"rows": [{"id": 1}]}},
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":{"rows":[{"id":1}]}}'

    def urlopen(*args, **kwargs):
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    sleeps = []
    client = TopvisorClient(
        credentials=TopvisorCredentials("u", "secret"), max_retries=1, sleep=sleeps.append
    )
    assert list(client.iter_pages("method", page_size=2)) == [{"id": 1}]
    assert sleeps == [0.0]


def test_invalid_credentials_error_never_contains_secret(monkeypatch):
    error = urllib.error.HTTPError("url", 401, "secret-key", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(error))
    client = TopvisorClient(credentials=TopvisorCredentials("u", "secret-key"), max_retries=0)
    with pytest.raises(TopvisorError) as raised:
        client.check_access()
    assert "secret-key" not in str(raised.value)

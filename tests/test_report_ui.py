from datetime import UTC, date, datetime

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import reverse

from apps.projects.models import Project
from apps.reports.models import GeneratedArtifact, Report, ReportDatasetSnapshot, ReportVersion
from apps.reports.services import create_report_version


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="reviewer", password="test-password")


@pytest.fixture
def project(db):
    return Project.objects.create(name="Универсальный проект", domain="example.test")


@pytest.mark.django_db
def test_work_pages_require_login(client, project):
    response = client.get(reverse("reports:report-list", args=[project.id]))
    assert response.status_code == 302
    assert response.url.startswith("/admin/login/")


@pytest.mark.django_db
def test_report_builder_uses_compact_spoilers_and_rich_work_editor(client, user, project):
    client.force_login(user)
    html = client.get(reverse("reports:report-list", args=[project.id])).content.decode()

    assert "Включены по умолчанию" not in html
    assert "по умолчанию скрыто" not in html
    assert "Настроить информационные и коммерческие разделы" in html
    assert "Настроить прорабатываемые категории" in html
    assert "Выбрать ПС для столбчатого графика" in html
    assert "Указать ссылки по поисковым системам и регионам" in html
    assert "Заполнить выполненные работы" in html
    assert "Хранение и очистка данных" in html
    assert "Точный выбор периодов" not in html
    assert 'data-rich-command="insertOrderedList"' in html
    assert "data-rich-link" in html


@pytest.mark.django_db
def test_report_creation_normalizes_month_and_reuses_report(client, user, project):
    client.force_login(user)
    url = reverse("reports:report-create", args=[project.id])
    first = client.post(url, {"month": "2026-07"})
    report = Report.objects.get()
    assert first.status_code == 302
    assert report.report_month == date(2026, 7, 1)
    second = client.post(url, {"month": "2026-07"})
    assert second.url == reverse("reports:report-detail", args=[report.id])
    assert Report.objects.count() == 1


@pytest.mark.django_db
def test_quick_project_create_reports_duplicate_normalized_domain_without_500(
    client, user, project
):
    client.force_login(user)
    response = client.post(
        reverse("reports:project-create"),
        {
            "name": "Дубликат",
            "domain": f"https://www.{project.domain}/",
            "position_provider": "serphunt",
        },
    )

    assert response.status_code == 400
    assert "Проект с таким доменом уже существует." in response.content.decode()
    assert Project.objects.count() == 1


@pytest.mark.django_db
def test_report_creation_freezes_project_specific_rich_work_text(client, user, project):
    client.force_login(user)
    response = client.post(
        reverse("reports:report-create", args=[project.id]),
        {
            "month": "2026-07",
            "include_completed_work": "on",
            "completed_work_text": (
                "<p><strong>Проверка</strong></p><ul><li>Готово</li></ul>"
                '<a href="https://example.test/result">Результат</a>'
            ),
        },
    )

    assert response.status_code == 302
    options = ReportDatasetSnapshot.objects.get().payload["display_options"]
    assert options["completed_work_text"].startswith("<p><strong>Проверка</strong>")
    assert "https://example.test/result" in options["completed_work_text"]
    assert project.report_settings.values["completed_work_text"] == options["completed_work_text"]


@pytest.mark.django_db
def test_version_creation_is_post_only_and_idempotent(client, user, project):
    client.force_login(user)
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    url = reverse("reports:version-create", args=[report.id])
    assert client.get(url).status_code == 405
    detail = client.get(reverse("reports:report-detail", args=[report.id]))
    token = detail.context["version_token"]
    assert client.post(url, {"token": token}).status_code == 302
    assert client.post(url, {"token": token}).status_code == 302
    assert ReportVersion.objects.count() == 1


@pytest.mark.django_db
def test_version_delete_is_accessible_post_only_and_removes_only_selected_version(
    client,
    user,
    project,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    old_version = create_report_version(report=report, created_by=user)
    current_version = create_report_version(report=report, created_by=user)
    old_snapshot_id = old_version.snapshot.pk
    artifact = GeneratedArtifact.objects.create(
        report_version=old_version,
        artifact_type=GeneratedArtifact.Type.PDF,
        status=GeneratedArtifact.Status.READY,
    )
    artifact.file.save("old.pdf", ContentFile(b"old-version"))
    artifact_storage, artifact_name = artifact.file.storage, artifact.file.name
    delete_url = reverse("reports:version-delete", args=[old_version.id])

    client.force_login(user)
    detail_html = client.get(reverse("reports:report-detail", args=[report.id])).content.decode()
    assert f'action="{delete_url}"' in detail_html
    assert 'aria-label="Удалить версию №1"' in detail_html
    assert client.get(delete_url).status_code == 405

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(delete_url)
    assert response.status_code == 302
    assert response.url == reverse("reports:report-detail", args=[report.id])
    assert not ReportVersion.objects.filter(pk=old_version.pk).exists()
    assert not ReportDatasetSnapshot.objects.filter(pk=old_snapshot_id).exists()
    assert not GeneratedArtifact.objects.filter(pk=artifact.pk).exists()
    assert not artifact_storage.exists(artifact_name)
    assert ReportVersion.objects.filter(pk=current_version.pk).exists()
    assert Report.objects.filter(pk=report.pk).exists()


@pytest.mark.django_db
def test_version_delete_requires_login_and_csrf(user, project):
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    version = create_report_version(report=report, created_by=user)
    url = reverse("reports:version-delete", args=[version.id])
    assert Client().post(url).status_code == 302
    protected_client = Client(enforce_csrf_checks=True)
    protected_client.force_login(user)
    assert protected_client.post(url).status_code == 403


@pytest.fixture
def preview_version(db, user, project):
    from apps.reports.models import NarrativeBlock, ReportDatasetSnapshot
    from apps.reports.services import snapshot_checksum

    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    version = ReportVersion.objects.create(report=report, number=1, created_by=user)
    change = {
        "current": "12.5",
        "previous": "10.0",
        "absolute": "2.5",
        "relative_percent": "25.0",
        "percentage_points": "2.5",
    }
    payload = {
        "schema_version": "test",
        "formula_version": "mvp1.3-position-metadata",
        "project": {"id": str(project.id), "name": project.name, "domain": project.domain},
        "periods": {
            "previous": {"start": "2026-06-01", "end": "2026-06-30"},
            "report": {"start": "2026-07-01", "end": "2026-07-31"},
        },
        "ranking_sources": [
            {
                "id": "safe-ranking-id",
                "date": "2026-07-31",
                "search_engine": "google",
                "region": "Россия",
                "ranking_depth": 20,
                "positions": [],
                "provenance": {
                    "method": "import",
                    "retrieved_at": "2026-08-01T10:00:00Z",
                    "response_checksum": "safe-checksum",
                    "api_key": "must-not-render",
                    "authorization": "Bearer must-not-render",
                },
            }
        ],
        "source_snapshots": [
            {
                "id": "safe-source-id",
                "source": "yandex_metrika",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "payload": {"oauth_token": "must-not-render"},
                "metrics": [{"code": "source_search_visits", "value": "80"}],
                "provenance": {"method": "api", "checksum": "source-checksum"},
            }
        ],
        "calculated": {
            "positions": {
                "segments": [
                    {
                        "search_engine": "google",
                        "region": "Россия",
                        "ranking_depth": 20,
                        "visibility_change": change,
                        "distribution": {
                            "ranges": {"1-3": 1, "4-10": 2, "11-20": 1},
                            "top_10": 3,
                            "top_30": None,
                            "total": 4,
                        },
                        "comparison_depth": 20,
                        "semantics": {
                            "previous_count": 3,
                            "current_count": 4,
                            "change_percent": "25",
                            "added": ["новый запрос"],
                            "removed": ["старый запрос"],
                        },
                        "warnings": [],
                        "top_11_20": [
                            {
                                "query": "seo отчёт",
                                "frequency": 100,
                                "position": 12,
                                "group": "Отчёты",
                                "target_url": "https://example.test/report/",
                            }
                        ],
                    },
                    {
                        "search_engine": "yandex",
                        "region": "Москва",
                        "ranking_depth": 10,
                        "visibility_change": change,
                        "distribution": {
                            "ranges": {"1-3": 2, "4-10": 1},
                            "top_10": 3,
                            "top_30": None,
                            "total": 3,
                        },
                        "comparison_depth": 10,
                        "semantics": {
                            "previous_count": 3,
                            "current_count": 3,
                            "change_percent": "0",
                            "added": [],
                            "removed": [],
                        },
                        "warnings": [],
                        "top_11_20": [],
                    },
                ]
            },
            "sources": {
                "sources": {
                    "yandex_metrika": {
                        "normalized_changes": {"visits": change},
                        "three_month_series": {
                            "visits": [
                                {"month": "2026-05-01", "value": "8"},
                                {"month": "2026-06-01", "value": "10"},
                                {"month": "2026-07-01", "value": "12.5"},
                            ]
                        },
                        "traffic_sources": {"total": "100", "shares": {"search": "80"}},
                    },
                    "yandex_webmaster": {"normalized_changes": {}, "three_month_series": {}},
                }
            },
        },
        "completed_work": [
            {
                "date": "2026-07-10",
                "category": "Контент",
                "title": "Обновление страницы",
                "status": "done",
                "url": "https://example.test/page/",
                "result_url": "https://example.test/result/",
                "responsible": "Редактор",
                "comment": "Готово",
                "provenance": {"method": "worklog"},
            }
        ],
    }
    ReportDatasetSnapshot.objects.create(
        version=version,
        schema_version="test",
        formula_version="mvp1.3-position-metadata",
        payload=payload,
        checksum=snapshot_checksum(payload),
    )
    for order, section in enumerate(
        (
            "visibility",
            "position_distribution",
            "top_10",
            "top_11_20",
            "position_dynamics",
            "traffic",
            "traffic_sources",
            "completed_work",
        )
    ):
        NarrativeBlock.objects.create(
            report_version=version,
            section_code=section,
            generated_text=f"Вывод {section}",
            facts={},
            sort_order=order,
        )
    return version


@pytest.mark.django_db
def test_preview_renders_snapshot_metrics_segments_work_without_source_diagnostics(
    client, user, preview_version
):
    client.force_login(user)
    preview_version.report.project.name = "Изменённое живое название"
    preview_version.report.project.save()
    response = client.get(reverse("reports:version-detail", args=[preview_version.id]))
    html = response.content.decode()
    for expected in (
        "12.5",
        "1-3",
        "Google",
        "Яндекс",
        "Россия",
        "Москва",
        "seo отчёт",
        "Отчёты",
        "https://example.test/report/",
        "2026-05-01",
        "Обновление страницы",
        "Универсальный проект",
    ):
        assert expected in html
    assert 'class="position-distribution"' in html
    assert "21-30" not in html and "31-50" not in html and "51-100" not in html
    assert "must-not-render" not in html and "safe-checksum" not in html
    assert "Источник данных" not in html
    assert "Показать вывод по запросам" in html
    assert "Google · Россия · 1 запрос" in html
    assert '<details class="keyword-spoiler">' in html
    assert '<details class="keyword-spoiler" open' not in html
    assert 'class="keyword-table with-url"' in html
    assert 'class="metric-change-table"' in html
    assert 'class="report-data-table visibility-table"' in html
    assert 'class="report-data-table position-dynamics-table"' in html
    assert 'class="report-data-table completed-work-table"' in html
    assert "80,0%" in html
    assert "<title>Версия 1 — Универсальный проект</title>" in html


@pytest.mark.django_db
def test_report_times_are_rendered_in_moscow_timezone(client, user, project):
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    version = create_report_version(report=report, created_by=user)
    ReportVersion.objects.filter(pk=version.pk).update(
        created_at=datetime(2026, 8, 20, 4, 52, tzinfo=UTC)
    )
    client.force_login(user)
    html = client.get(reverse("reports:report-detail", args=[report.id])).content.decode()
    assert "20.08.2026 07:52" in html


@pytest.mark.django_db
def test_edit_confirm_reset_and_html_escaping(client, user, preview_version):
    from apps.reports.models import NarrativeBlock

    client.force_login(user)
    block = preview_version.narrative_blocks.get(section_code="visibility")
    generated, facts = block.generated_text, block.facts
    edit_url = reverse("reports:narrative-edit", args=[block.id])
    response = client.post(
        edit_url, {"edited_text": "<script>alert(1)</script>"}, HTTP_HX_REQUEST="true"
    )
    block.refresh_from_db()
    assert response.status_code == 200
    assert block.status == NarrativeBlock.Status.EDITED
    assert block.generated_text == generated and block.facts == facts
    assert "&lt;script&gt;" in response.content.decode()

    client.post(reverse("reports:narrative-confirm", args=[block.id]))
    block.refresh_from_db()
    assert block.status == NarrativeBlock.Status.CONFIRMED
    assert block.confirmed_by == user and block.confirmed_at is not None

    client.post(edit_url, {"edited_text": "Новая редакция"})
    block.refresh_from_db()
    assert block.status == NarrativeBlock.Status.EDITED
    assert block.confirmed_by is None and block.confirmed_at is None

    client.post(reverse("reports:narrative-reset", args=[block.id]))
    block.refresh_from_db()
    assert block.status == NarrativeBlock.Status.GENERATED
    assert block.edited_text == "" and block.effective_text == generated


@pytest.mark.django_db
def test_invalid_htmx_edit_keeps_full_preview_and_marks_form(client, user, preview_version):
    client.force_login(user)
    block = preview_version.narrative_blocks.get(section_code="visibility")
    response = client.post(
        reverse("reports:narrative-edit", args=[block.id]),
        {"edited_text": "x" * 10_001},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert "Контроль качества" in html
    assert "Выполненные работы" in html
    assert "Убедитесь, что это значение содержит не более 10000 символов" in html


@pytest.mark.django_db
def test_mutating_narrative_and_validation_endpoints_reject_get(client, user, preview_version):
    client.force_login(user)
    block = preview_version.narrative_blocks.first()
    urls = (
        reverse("reports:validate", args=[preview_version.id]),
        reverse("reports:narrative-edit", args=[block.id]),
        reverse("reports:narrative-reset", args=[block.id]),
        reverse("reports:narrative-confirm", args=[block.id]),
    )
    assert all(client.get(url).status_code == 405 for url in urls)


@pytest.mark.django_db
def test_edit_runs_validator(client, user, preview_version, monkeypatch):
    client.force_login(user)
    block = preview_version.narrative_blocks.get(section_code="visibility")
    called = []
    monkeypatch.setattr("apps.reports.views.validate_report_version", called.append)
    client.post(
        reverse("reports:narrative-edit", args=[block.id]), {"edited_text": "Проверенный вывод"}
    )
    assert called == [block.report_version]


def _replace_position_segments(version, segments):
    from copy import deepcopy

    from apps.reports.models import ReportDatasetSnapshot
    from apps.reports.services import snapshot_checksum

    payload = deepcopy(version.snapshot.payload)
    payload["calculated"]["positions"]["segments"] = segments
    ReportDatasetSnapshot.objects.filter(version=version).update(
        payload=payload, checksum=snapshot_checksum(payload)
    )


@pytest.mark.django_db
def test_google_top_20_has_no_top_30_or_deeper_range(client, user, preview_version):
    client.force_login(user)
    google = preview_version.snapshot.payload["calculated"]["positions"]["segments"][0]
    _replace_position_segments(preview_version, [google])
    html = client.get(reverse("reports:version-detail", args=[preview_version.id])).content.decode()
    assert "TOP-30" not in html
    assert "21-30" not in html


@pytest.mark.django_db
def test_top_30_is_rendered_for_confirmed_depth(client, user, preview_version):
    client.force_login(user)
    segment = preview_version.snapshot.payload["calculated"]["positions"]["segments"][0]
    segment = {**segment, "ranking_depth": 30}
    segment["distribution"] = {
        **segment["distribution"],
        "ranges": {"1-3": 1, "4-10": 2, "11-20": 1, "21-30": 1},
        "top_30": 5,
        "total": 5,
    }
    _replace_position_segments(preview_version, [segment])
    html = client.get(reverse("reports:version-detail", args=[preview_version.id])).content.decode()
    assert "<th>TOP-30</th>" in html
    assert "<td>5</td>" in html


@pytest.mark.django_db
def test_mixed_depth_shows_top_30_only_in_confirmed_segment(client, user, preview_version):
    client.force_login(user)
    segments = preview_version.snapshot.payload["calculated"]["positions"]["segments"]
    google = segments[0]
    yandex = {**segments[1], "ranking_depth": 100}
    yandex["distribution"] = {
        **yandex["distribution"],
        "ranges": {"1-3": 2, "4-10": 1, "11-20": 1, "21-30": 2},
        "top_30": 6,
        "total": 6,
    }
    _replace_position_segments(preview_version, [google, yandex])
    html = client.get(reverse("reports:version-detail", args=[preview_version.id])).content.decode()
    google_article = html.split('data-engine="google"', 1)[1].split("</article>", 1)[0]
    yandex_article = html.split('data-engine="yandex"', 1)[1].split("</article>", 1)[0]
    assert "TOP-30" not in google_article
    assert "TOP-30" in yandex_article


@pytest.mark.django_db
def test_report_and_version_navigation_keep_current_project(client, user, preview_version):
    client.force_login(user)
    report_response = client.get(reverse("reports:report-detail", args=[preview_version.report_id]))
    version_response = client.get(reverse("reports:version-detail", args=[preview_version.id]))
    expected_url = reverse("reports:report-list", args=[preview_version.report.project_id])
    assert report_response.context["project"] == preview_version.report.project
    assert version_response.context["project"] == preview_version.report.project
    assert expected_url in report_response.content.decode()
    assert expected_url in version_response.content.decode()

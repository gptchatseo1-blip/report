from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.projects.models import Project
from apps.reports.models import Report, ReportVersion


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
def test_preview_renders_snapshot_metrics_segments_work_and_safe_provenance(
    client, user, preview_version
):
    client.force_login(user)
    preview_version.report.project.name = "Изменённое живое название"
    preview_version.report.project.save()
    response = client.get(reverse("reports:version-detail", args=[preview_version.id]))
    html = response.content.decode()
    for expected in (
        "12.5",
        "1-3: 1",
        "Google",
        "Яндекс",
        "Россия",
        "Москва",
        "seo отчёт",
        "Отчёты",
        "https://example.test/report/",
        "2026-05-01",
        "Обновление страницы",
        "safe-checksum",
        "Универсальный проект",
    ):
        assert expected in html
    assert "21-30" not in html and "31-50" not in html and "51-100" not in html
    assert "must-not-render" not in html
    assert "Изменённое живое название" not in html


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

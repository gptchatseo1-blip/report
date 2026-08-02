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

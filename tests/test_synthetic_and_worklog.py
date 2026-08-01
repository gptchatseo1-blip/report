from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.metrics.models import MetricPoint, SourceSnapshot
from apps.metrics.synthetic import build_synthetic_payload, sync_synthetic_metrics
from apps.projects.models import Project
from apps.worklog.models import WorkCategory, WorkLogItem

pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    return Project.objects.create(name="Demo", domain="example.com")


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="admin", email="admin@example.com", password="safe-test-password"
    )
    client.force_login(user)
    client.user = user
    return client


def test_synthetic_payload_is_deterministic(project):
    month = date(2026, 7, 1)
    first = build_synthetic_payload(project, SourceSnapshot.Source.METRIKA, month)
    second = build_synthetic_payload(project, SourceSnapshot.Source.METRIKA, month)

    assert first == second
    assert first["retrieval_method"] == "synthetic"
    assert first["project_domain"] == "example.com"


def test_sync_creates_two_sources_for_three_months_idempotently(project, staff_client):
    snapshots, created_count = sync_synthetic_metrics(
        project=project, report_month=date(2026, 7, 1), user=staff_client.user
    )

    assert len(snapshots) == 6
    assert created_count == 6
    assert SourceSnapshot.objects.count() == 6
    assert {snapshot.period_start for snapshot in snapshots} == {
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    }
    first_checksums = list(SourceSnapshot.objects.order_by("id").values_list("checksum", flat=True))

    _, second_created_count = sync_synthetic_metrics(
        project=project, report_month=date(2026, 7, 1), user=staff_client.user
    )
    assert second_created_count == 0
    assert SourceSnapshot.objects.count() == 6
    assert (
        list(SourceSnapshot.objects.order_by("id").values_list("checksum", flat=True))
        == first_checksums
    )


def test_metrika_source_visits_add_up_to_total(project):
    snapshots, _ = sync_synthetic_metrics(project=project, report_month=date(2026, 7, 1))
    snapshot = next(
        item
        for item in snapshots
        if item.source == SourceSnapshot.Source.METRIKA and item.period_start == date(2026, 7, 1)
    )
    metrics = dict(snapshot.metrics.values_list("metric_code", "numeric_value"))
    source_total = sum(value for code, value in metrics.items() if code.startswith("source_"))
    assert source_total == metrics["visits"]
    assert metrics["users"] <= metrics["visits"]


def test_webmaster_ctr_matches_clicks_and_impressions(project):
    snapshots, _ = sync_synthetic_metrics(project=project, report_month=date(2026, 7, 1))
    snapshot = next(
        item
        for item in snapshots
        if item.source == SourceSnapshot.Source.WEBMASTER and item.period_start == date(2026, 7, 1)
    )
    metrics = dict(snapshot.metrics.values_list("metric_code", "numeric_value"))
    expected_ctr = (metrics["search_clicks"] / metrics["search_impressions"] * 100).quantize(
        Decimal("0.01")
    )
    assert metrics["search_ctr"].quantize(Decimal("0.01")) == expected_ctr


def test_work_item_requires_category_from_same_project(project):
    other_project = Project.objects.create(name="Other", domain="other.example.com")
    category = WorkCategory.objects.create(project=other_project, name="Контент")
    item = WorkLogItem(
        project=project,
        work_date=date(2026, 7, 15),
        category=category,
        title="Подготовлена статья",
    )
    with pytest.raises(ValidationError, match="Категория должна принадлежать"):
        item.full_clean()


def test_staff_user_can_create_category_and_work_item(project, staff_client):
    response = staff_client.post(
        reverse("worklog:category_create"),
        {"project": project.id, "name": "Контент", "sort_order": 10, "active": True},
    )
    assert response.status_code == 302
    category = WorkCategory.objects.get()

    response = staff_client.post(
        reverse("worklog:create"),
        {
            "project": project.id,
            "work_date": "2026-07-15",
            "category": category.id,
            "title": "Подготовлена статья",
            "status": WorkLogItem.Status.COMPLETED,
            "responsible": "SEO-специалист",
            "comment": "Материал опубликован",
        },
    )
    assert response.status_code == 302
    item = WorkLogItem.objects.get()
    assert item.created_by == staff_client.user
    assert item.category == category


def test_synthetic_and_worklog_pages_require_staff_authentication(client):
    for url_name in ("metrics:synthetic_sync", "worklog:list", "worklog:create"):
        response = client.get(reverse(url_name))
        assert response.status_code == 302
        assert reverse("admin:login") in response.url


def test_synthetic_sync_page_creates_snapshots(project, staff_client):
    response = staff_client.post(
        reverse("metrics:synthetic_sync"),
        {"project": project.id, "report_month": "2026-07"},
    )
    assert response.status_code == 302
    assert SourceSnapshot.objects.count() == 6
    assert MetricPoint.objects.filter(snapshot__source=SourceSnapshot.Source.METRIKA).exists()

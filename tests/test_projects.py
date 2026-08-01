import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.projects.forms import ProjectForm
from apps.projects.models import Project, ProjectBrandRule, ProjectUrlGroup, ProjectUrlRule
from apps.projects.services import classify_url

pytestmark = pytest.mark.django_db


def test_project_create_and_edit_normalizes_domain():
    form = ProjectForm(
        {
            "name": "Demo",
            "domain": "https://WWW.Пример.РФ/path",
            "timezone": "UTC",
            "language": "ru",
            "active": True,
        }
    )
    assert form.is_valid(), form.errors
    project = form.save()
    assert project.normalized_domain == "xn--e1afmkfd.xn--p1ai"
    form = ProjectForm(
        {
            "name": "Renamed",
            "domain": "example.org.",
            "timezone": "UTC",
            "language": "ru",
            "active": True,
        },
        instance=project,
    )
    assert form.is_valid(), form.errors
    assert form.save().normalized_domain == "example.org"


def test_normalized_domain_is_unique():
    Project.objects.create(name="One", domain="https://www.example.com")
    with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
        Project.objects.create(name="Two", domain="EXAMPLE.COM/")


def test_brand_rules_are_case_insensitive_and_unique():
    project = Project.objects.create(name="Demo", domain="example.com")
    rule = ProjectBrandRule.objects.create(project=project, kind="literal", pattern="Бренд")
    assert rule.matches("купить БРЕНД сегодня")
    with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
        ProjectBrandRule.objects.create(project=project, kind="literal", pattern="бренд")


def test_unsafe_regex_is_rejected():
    project = Project.objects.create(name="Demo", domain="example.com")
    with pytest.raises(ValidationError):
        ProjectBrandRule.objects.create(project=project, kind="regex", pattern="(a+)+$")


def test_url_group_generates_unicode_slug_from_russian_name():
    project = Project.objects.create(name="Demo", domain="example.com")
    group = ProjectUrlGroup.objects.create(project=project, name="Каталог услуг")
    assert group.slug == "каталог-услуг"


def test_overlapping_url_groups_use_highest_priority_and_report_diagnostic():
    project = Project.objects.create(name="Demo", domain="example.com")
    broad = ProjectUrlGroup.objects.create(
        project=project, name="Catalog", slug="catalog", priority=10
    )
    exact = ProjectUrlGroup.objects.create(project=project, name="Shoes", slug="shoes", priority=20)
    ProjectUrlRule.objects.create(
        group=broad, type="starts_with", pattern="https://example.com/catalog/"
    )
    ProjectUrlRule.objects.create(group=exact, type="contains", pattern="/catalog/shoes/")
    result = classify_url(project, "https://example.com/catalog/shoes/red")
    assert result.group == exact
    assert result.has_overlap
    assert result.overlapping_groups == (exact, broad)


def test_admin_requires_authentication(client):
    response = client.get(reverse("admin:index"))
    assert response.status_code == 302
    assert reverse("admin:login") in response.url


def test_health_check(client):
    response = client.get(reverse("health"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

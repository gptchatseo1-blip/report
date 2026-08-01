import re
import uuid
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.text import slugify

from .validators import validate_safe_regex


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("Некорректное доменное имя.") from exc


class TimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Project(TimestampedModel):
    name = models.CharField("Название", max_length=200)
    domain = models.CharField("Домен", max_length=253)
    normalized_domain = models.CharField(max_length=253, unique=True, editable=False)
    timezone = models.CharField("Часовой пояс", max_length=64, default="Europe/Moscow")
    language = models.CharField("Язык", max_length=10, default="ru")
    active = models.BooleanField("Активен", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Проект"
        verbose_name_plural = "Проекты"

    def clean(self):
        super().clean()
        self.normalized_domain = normalize_domain(self.domain)
        if not self.normalized_domain or "." not in self.normalized_domain:
            raise ValidationError({"domain": "Укажите корректный домен."})

    def save(self, *args, **kwargs):
        self.normalized_domain = normalize_domain(self.domain)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.normalized_domain})"


class ProjectBrandRule(TimestampedModel):
    class Kind(models.TextChoices):
        LITERAL = "literal", "Строка"
        REGEX = "regex", "Регулярное выражение"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="brand_rules")
    kind = models.CharField("Тип", max_length=10, choices=Kind.choices, default=Kind.LITERAL)
    pattern = models.CharField("Шаблон", max_length=500)
    priority = models.IntegerField("Приоритет", default=0)
    active = models.BooleanField("Активно", default=True)

    class Meta:
        ordering = ["-priority", "pattern"]
        verbose_name = "Брендовое правило"
        verbose_name_plural = "Брендовые правила"
        constraints = [
            models.UniqueConstraint(
                Lower("pattern"), "project", "kind", name="unique_brand_rule_case_insensitive"
            )
        ]

    def clean(self):
        super().clean()
        self.pattern = self.pattern.strip()
        if self.kind == self.Kind.REGEX:
            validate_safe_regex(self.pattern)

        if self.project_id and self.pattern:
            rules = type(self).objects.filter(project_id=self.project_id, kind=self.kind)
            if self.pk:
                rules = rules.exclude(pk=self.pk)
            if any(rule.pattern.casefold() == self.pattern.casefold() for rule in rules.only("pattern")):
                raise ValidationError(
                    {"pattern": "Такое брендовое правило уже существует в этом проекте."}
                )

    def matches(self, query: str) -> bool:
        if not self.active:
            return False
        if self.kind == self.Kind.LITERAL:
            return self.pattern.casefold() in query.casefold()
        return re.search(self.pattern, query, re.IGNORECASE) is not None

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.pattern


class ProjectUrlGroup(TimestampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="url_groups")
    name = models.CharField("Название", max_length=200)
    slug = models.SlugField("Код", max_length=200, blank=True, allow_unicode=True)
    priority = models.IntegerField("Приоритет", default=0)
    active = models.BooleanField("Активна", default=True)

    class Meta:
        ordering = ["-priority", "name"]
        verbose_name = "URL-группа"
        verbose_name_plural = "URL-группы"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug"], name="unique_project_url_group_slug"
            )
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ProjectUrlRule(TimestampedModel):
    class Type(models.TextChoices):
        STARTS_WITH = "starts_with", "Начинается с"
        CONTAINS = "contains", "Содержит"
        REGEX = "regex", "Регулярное выражение"

    group = models.ForeignKey(ProjectUrlGroup, on_delete=models.CASCADE, related_name="rules")
    type = models.CharField("Тип", max_length=16, choices=Type.choices)
    pattern = models.CharField("Шаблон", max_length=500)
    priority = models.IntegerField("Приоритет", default=0)
    active = models.BooleanField("Активно", default=True)

    class Meta:
        ordering = ["-priority", "pattern"]
        verbose_name = "URL-правило"
        verbose_name_plural = "URL-правила"
        constraints = [
            models.UniqueConstraint(
                fields=["group", "type", "pattern"], name="unique_group_url_rule"
            )
        ]

    def clean(self):
        super().clean()
        self.pattern = self.pattern.strip()
        if self.type == self.Type.REGEX:
            validate_safe_regex(self.pattern)

    def matches(self, url: str) -> bool:
        if not self.active:
            return False
        if self.type == self.Type.STARTS_WITH:
            return url.startswith(self.pattern)
        if self.type == self.Type.CONTAINS:
            return self.pattern in url
        return re.search(self.pattern, url) is not None

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.pattern

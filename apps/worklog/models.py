import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.text import slugify

from apps.projects.models import Project


class WorkCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="work_categories")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True, allow_unicode=True)
    sort_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Категория работ"
        verbose_name_plural = "Категории работ"
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "project", name="unique_project_work_category_name"
            ),
            models.UniqueConstraint(
                fields=["project", "slug"], name="unique_project_work_category_slug"
            ),
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.project.name}: {self.name}"


class WorkLogItem(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Выполнено"
        IN_PROGRESS = "in_progress", "В работе"
        PLANNED = "planned", "Запланировано"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="worklog_items")
    work_date = models.DateField("Дата работы")
    category = models.ForeignKey(
        WorkCategory, on_delete=models.PROTECT, related_name="items", verbose_name="Категория"
    )
    title = models.CharField("Работа", max_length=300)
    status = models.CharField(
        "Статус", max_length=16, choices=Status.choices, default=Status.COMPLETED
    )
    url = models.URLField("URL страницы", max_length=2000, blank=True)
    page_or_material_name = models.CharField("Страница или материал", max_length=300, blank=True)
    character_count = models.PositiveIntegerField("Количество знаков", null=True, blank=True)
    responsible = models.CharField("Ответственный", max_length=200, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    result_url = models.URLField("Ссылка на результат", max_length=2000, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_worklog_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "category__sort_order", "title"]
        verbose_name = "Выполненная работа"
        verbose_name_plural = "Выполненные работы"
        indexes = [models.Index(fields=["project", "work_date"], name="worklog_project_date_idx")]

    def clean(self):
        super().clean()
        self.title = self.title.strip()
        if self.project_id and self.category_id and self.category.project_id != self.project_id:
            raise ValidationError({"category": "Категория должна принадлежать выбранному проекту."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.work_date}: {self.title}"

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.projects.models import Project


class Report(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="reports")
    report_month = models.DateField("Отчётный месяц")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-report_month", "project__name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "report_month"], name="unique_report_month")
        ]

    def clean(self):
        if self.report_month and self.report_month.day != 1:
            raise ValidationError({"report_month": "Укажите первый день календарного месяца."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.project} — {self.report_month:%m.%Y}"


class ReportVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["report", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["report", "number"], name="unique_report_version")
        ]

    def __str__(self):
        return f"{self.report}, версия {self.number}"


class ReportDatasetSnapshot(models.Model):
    """The durable boundary: application code may create, but never mutate, this row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.OneToOneField(ReportVersion, on_delete=models.CASCADE, related_name="snapshot")
    schema_version = models.CharField(max_length=32)
    formula_version = models.CharField(max_length=64)
    payload = models.JSONField()
    checksum = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        raise ValidationError("Зафиксированный snapshot нельзя изменять.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Зафиксированный snapshot нельзя удалять.")

    def __str__(self):
        return f"{self.version} — {self.checksum[:12]}"


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        WARNING = "warning", "Предупреждение"
        ERROR = "error", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        ReportVersion, on_delete=models.CASCADE, related_name="validation_issues"
    )
    code = models.CharField(max_length=80)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.WARNING)
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["severity", "code"]

    def __str__(self):
        return f"{self.code}: {self.version}"

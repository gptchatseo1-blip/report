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
        if not self._state.adding:
            original = type(self).objects.only("project_id", "report_month").get(pk=self.pk)
            changed = {}
            if original.project_id != self.project_id:
                changed["project"] = "Нельзя изменить проект отчёта после создания версии."
            if original.report_month != self.report_month:
                changed["report_month"] = "Нельзя изменить отчётный месяц после создания версии."
            if changed and self.versions.exists():
                raise ValidationError(changed)
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

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.only("report_id", "number").get(pk=self.pk)
            changed = {}
            if original.report_id != self.report_id:
                changed["report"] = "Нельзя изменить отчёт существующей версии."
            if original.number != self.number:
                changed["number"] = "Нельзя изменить номер существующей версии."
            if changed:
                raise ValidationError(changed)
        return super().save(*args, **kwargs)


class ReportDatasetSnapshot(models.Model):
    """The durable boundary: application code may create, but never mutate, this row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.OneToOneField(ReportVersion, on_delete=models.PROTECT, related_name="snapshot")
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


class NarrativeBlock(models.Model):
    class Kind(models.TextChoices):
        DETERMINISTIC = "deterministic", "Детерминированный"
        MANUAL = "manual", "Ручной"

    class Status(models.TextChoices):
        GENERATED = "generated", "Сформирован"
        EDITED = "edited", "Отредактирован"
        CONFIRMED = "confirmed", "Подтверждён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_version = models.ForeignKey(
        ReportVersion, on_delete=models.CASCADE, related_name="narrative_blocks"
    )
    section_code = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.DETERMINISTIC)
    generated_text = models.TextField(blank=True)
    edited_text = models.TextField(blank=True)
    facts = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.GENERATED)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["report_version", "section_code", "sort_order"],
                name="unique_narrative_block_order",
            )
        ]

    IMMUTABLE_FIELDS = (
        "report_version_id",
        "section_code",
        "kind",
        "generated_text",
        "facts",
        "sort_order",
    )

    @property
    def effective_text(self):
        return self.edited_text if self.edited_text.strip() else self.generated_text

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            changed = {
                field: "Сгенерированные данные narrative нельзя изменять после создания."
                for field in self.IMMUTABLE_FIELDS
                if getattr(original, field) != getattr(self, field)
            }
            if changed:
                raise ValidationError(changed)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.report_version}: {self.section_code} ({self.sort_order})"


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        WARNING = "warning", "Предупреждение"
        ERROR = "error", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        ReportVersion, on_delete=models.CASCADE, related_name="validation_issues"
    )
    code = models.CharField(max_length=80)
    section_code = models.CharField(max_length=80, blank=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.WARNING)
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["severity", "code"]

    def __str__(self):
        return f"{self.code}: {self.version}"


def artifact_upload_to(instance, filename):
    """Keep storage names derived exclusively from trusted UUIDs."""
    return (
        f"reports/{instance.report_version.report.project_id}/"
        f"{instance.report_version_id}/{filename}"
    )


class GeneratedArtifact(models.Model):
    class Type(models.TextChoices):
        DOCX = "docx", "DOCX"
        PDF = "pdf", "PDF"
        XLSX = "xlsx", "XLSX"

    class Status(models.TextChoices):
        GENERATING = "generating", "Формируется"
        READY = "ready", "Готов"
        FAILED = "failed", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_version = models.ForeignKey(
        ReportVersion, on_delete=models.CASCADE, related_name="generated_artifacts"
    )
    artifact_type = models.CharField(max_length=8, choices=Type.choices)
    profile = models.CharField(max_length=16, default="full", editable=False)
    is_draft = models.BooleanField(default=False)
    file = models.FileField(upload_to=artifact_upload_to, blank=True, max_length=255)
    filename = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    generator_version = models.CharField(max_length=32, default="mvp1.0")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.GENERATING)
    generation_log = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename or f"{self.artifact_type}: {self.report_version}"

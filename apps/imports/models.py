import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from apps.projects.models import Project


def import_upload_path(instance, filename):
    safe_name = Path(filename).name
    return f"imports/{instance.project_id}/{uuid.uuid4()}_{safe_name}"


class ImportBatch(models.Model):
    class Kind(models.TextChoices):
        TOPVISOR_POSITIONS = "topvisor_positions", "Позиции Topvisor"
        FILE_POSITIONS = "file_positions", "Позиции из файла"

    class Status(models.TextChoices):
        PREVIEW = "preview", "Ожидает подтверждения"
        IMPORTED = "imported", "Импортирован"
        FAILED = "failed", "Ошибка"

    class SearchEngine(models.TextChoices):
        YANDEX = "yandex", "Яндекс"
        GOOGLE = "google", "Google"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="import_batches")
    segment = models.ForeignKey(
        "FileImportSegment",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="batches",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.TOPVISOR_POSITIONS)
    original_filename = models.CharField(max_length=255)
    source_file = models.FileField(upload_to=import_upload_path)
    file_checksum = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PREVIEW)
    snapshot_date = models.DateField()
    search_engine = models.CharField(max_length=16, choices=SearchEngine.choices)
    region = models.CharField(max_length=120)
    ranking_depth = models.PositiveSmallIntegerField(
        "Глубина проверки", choices=[(x, f"ТОП-{x}") for x in (10, 20, 30, 50, 100)], default=100
    )
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    date_count = models.PositiveIntegerField(default=1)
    preview_payload = models.JSONField(default=list, blank=True)
    error_summary = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="position_imports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Партия импорта"
        verbose_name_plural = "Партии импорта"
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "project",
                    "kind",
                    "file_checksum",
                    "snapshot_date",
                    "search_engine",
                    "region",
                    "ranking_depth",
                ],
                name="unique_position_import_input",
            )
        ]

    def __str__(self):
        return f"{self.project}: {self.original_filename} ({self.snapshot_date})"

    @property
    def ranking_snapshot(self):
        """Compatibility accessor for legacy single-date imports."""
        return self.ranking_snapshots.order_by("snapshot_date").first()


class FileImportSegment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="file_import_segments"
    )
    search_engine = models.CharField(max_length=16, choices=ImportBatch.SearchEngine.choices)
    region = models.CharField(max_length=120)
    calculate_visibility = models.BooleanField(default=True)
    last_filename = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    keyword_count = models.PositiveIntegerField(default=0)
    date_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=ImportBatch.Status.choices, default=ImportBatch.Status.PREVIEW
    )
    error_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["search_engine", "region", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "search_engine", "region"],
                name="unique_file_import_segment",
            )
        ]

    @property
    def configuration_id(self):
        return f"file:{self.id}"

    def __str__(self):
        return f"{self.get_search_engine_display()} · {self.region}"


class ImportRowError(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="row_errors")
    row_number = models.PositiveIntegerField()
    code = models.CharField(max_length=64)
    message = models.CharField(max_length=500)
    raw_values = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["row_number", "code"]
        verbose_name = "Ошибка строки импорта"
        verbose_name_plural = "Ошибки строк импорта"
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number", "code"], name="unique_import_row_error"
            )
        ]

    def __str__(self):
        return f"Строка {self.row_number}: {self.message}"

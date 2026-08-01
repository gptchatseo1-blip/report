import uuid

from django.db import models

from apps.imports.models import ImportBatch
from apps.projects.models import Project


class RankingSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ranking_snapshots")
    import_batch = models.OneToOneField(
        ImportBatch, on_delete=models.PROTECT, related_name="ranking_snapshot"
    )
    snapshot_date = models.DateField()
    search_engine = models.CharField(max_length=16)
    region = models.CharField(max_length=120)
    tracked_keyword_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-snapshot_date", "search_engine", "region"]
        verbose_name = "Снимок позиций"
        verbose_name_plural = "Снимки позиций"
        indexes = [
            models.Index(
                fields=["project", "snapshot_date", "search_engine", "region"],
                name="ranking_lookup_idx",
            )
        ]

    def __str__(self):
        return f"{self.project} — {self.snapshot_date} — {self.search_engine}"


class KeywordPosition(models.Model):
    class Status(models.TextChoices):
        RANKED = "ranked", "Есть позиция"
        BEYOND_100 = "beyond_100", "За пределами топ-100"
        NOT_FOUND = "not_found", "Не найден"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ranking_snapshot = models.ForeignKey(
        RankingSnapshot, on_delete=models.CASCADE, related_name="positions"
    )
    query = models.CharField(max_length=500)
    normalized_query = models.CharField(max_length=500)
    frequency = models.PositiveIntegerField()
    position_raw = models.CharField(max_length=50, blank=True)
    position_value = models.PositiveSmallIntegerField(null=True, blank=True)
    position_status = models.CharField(max_length=16, choices=Status.choices)
    group_name = models.CharField(max_length=255, blank=True)
    target_url = models.CharField(max_length=2000, blank=True)
    normalized_target_url = models.CharField(max_length=2000, blank=True)

    class Meta:
        ordering = ["position_status", "position_value", "normalized_query"]
        verbose_name = "Позиция запроса"
        verbose_name_plural = "Позиции запросов"
        constraints = [
            models.UniqueConstraint(
                fields=["ranking_snapshot", "normalized_query", "group_name"],
                name="unique_snapshot_query_group",
            )
        ]
        indexes = [
            models.Index(fields=["ranking_snapshot", "position_status"], name="position_status_idx")
        ]

    def __str__(self):
        return f"{self.query}: {self.position_raw or self.get_position_status_display()}"

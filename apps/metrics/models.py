import uuid

from django.conf import settings
from django.db import models

from apps.imports.models import ImportBatch
from apps.projects.models import Project


class RankingSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ranking_snapshots")
    import_batch = models.OneToOneField(
        ImportBatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ranking_snapshot",
    )
    snapshot_date = models.DateField()
    search_engine = models.CharField(max_length=16)
    region = models.CharField(max_length=120)
    tracked_keyword_count = models.PositiveIntegerField(default=0)
    ranking_depth = models.PositiveSmallIntegerField(default=100)
    depth_raw = models.CharField(max_length=100, default="100")
    depth_retrieved_at = models.DateTimeField(null=True, blank=True)

    class DepthSource(models.TextChoices):
        TOPVISOR_API = "topvisor_api", "Topvisor API"
        MANUAL = "manual", "Ручной импорт"

    depth_source = models.CharField(
        max_length=16, choices=DepthSource.choices, default=DepthSource.MANUAL
    )
    topvisor_configuration_id = models.CharField(max_length=120, blank=True)
    visibility = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    visibility_raw = models.JSONField(null=True, blank=True)
    response_checksum = models.CharField(max_length=64, blank=True)
    retrieved_at = models.DateTimeField(null=True, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
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
        constraints = [
            models.UniqueConstraint(
                fields=["project", "snapshot_date", "topvisor_configuration_id"],
                condition=~models.Q(topvisor_configuration_id=""),
                name="unique_topvisor_ranking_snapshot",
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


class SourceSnapshot(models.Model):
    class Source(models.TextChoices):
        METRIKA = "yandex_metrika", "Яндекс Метрика"
        WEBMASTER = "yandex_webmaster", "Яндекс Вебмастер"

    class RetrievalMethod(models.TextChoices):
        SYNTHETIC = "synthetic", "Синтетические данные"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="source_snapshots")
    source = models.CharField(max_length=32, choices=Source.choices)
    retrieval_method = models.CharField(
        max_length=16, choices=RetrievalMethod.choices, default=RetrievalMethod.SYNTHETIC
    )
    period_start = models.DateField()
    period_end = models.DateField()
    payload = models.JSONField(default=dict)
    checksum = models.CharField(max_length=64)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_source_snapshots",
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start", "source"]
        verbose_name = "Снимок источника"
        verbose_name_plural = "Снимки источников"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "source", "period_start", "period_end"],
                name="unique_project_source_period",
            )
        ]

    def __str__(self):
        return f"{self.project} — {self.get_source_display()} — {self.period_start:%m.%Y}"


class MetricPoint(models.Model):
    class Unit(models.TextChoices):
        COUNT = "count", "Количество"
        PERCENT = "percent", "Процент"
        SECONDS = "seconds", "Секунды"
        NUMBER = "number", "Число"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.CASCADE, related_name="metrics")
    metric_code = models.CharField(max_length=80)
    numeric_value = models.DecimalField(max_digits=18, decimal_places=4)
    unit = models.CharField(max_length=16, choices=Unit.choices)
    dimensions = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["snapshot", "metric_code"]
        verbose_name = "Показатель"
        verbose_name_plural = "Показатели"
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "metric_code"], name="unique_snapshot_metric_code"
            )
        ]
        indexes = [models.Index(fields=["metric_code"], name="metric_code_idx")]

    def __str__(self):
        return f"{self.metric_code}: {self.numeric_value}"

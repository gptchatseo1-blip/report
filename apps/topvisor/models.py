from django.db import models

from apps.projects.models import Project


class TopvisorProjectMapping(models.Model):
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="topvisor_mapping"
    )
    topvisor_project_id = models.CharField("ID проекта Topvisor", max_length=64)
    topvisor_project_name = models.CharField("Проект Topvisor", max_length=255, blank=True)
    selected_configurations = models.JSONField(
        "Конфигурации поиска", default=list, help_text="ID конкретных поисковой системы и региона"
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Сопоставление Topvisor"
        verbose_name_plural = "Сопоставления Topvisor"

    def __str__(self):
        return f"{self.project} → {self.topvisor_project_name or self.topvisor_project_id}"


class TopvisorSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCESS = "success", "Завершена"
        FAILED = "failed", "Ошибка"

    mapping = models.ForeignKey(
        TopvisorProjectMapping, on_delete=models.CASCADE, related_name="sync_runs"
    )
    report_month = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    loaded_keyword_count = models.PositiveIntegerField(default=0)
    segments = models.JSONField(default=list, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.mapping} — {self.report_month:%m.%Y}: {self.status}"

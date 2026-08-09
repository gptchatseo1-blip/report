import uuid

from django.conf import settings
from django.db import models

from apps.projects.models import Project


class YandexConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="yandex_connections"
    )
    account_id = models.CharField(max_length=255, blank=True)
    account_login = models.CharField(max_length=255, blank=True)
    access_token_encrypted = models.BinaryField(editable=False)
    refresh_token_encrypted = models.BinaryField(null=True, blank=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.account_login or "Аккаунт Яндекса"


class YandexOAuthState(models.Model):
    digest = models.CharField(max_length=64, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session_key = models.CharField(max_length=40)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"OAuth state for {self.user_id}"


class YandexMetrikaProjectMapping(models.Model):
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="yandex_metrika_mapping"
    )
    connection = models.ForeignKey(
        YandexConnection, on_delete=models.PROTECT, related_name="metrika_mappings"
    )
    counter_id = models.CharField(max_length=64)
    counter_name = models.CharField(max_length=255)
    counter_domain = models.CharField(max_length=253)
    selected_goals = models.JSONField(default=list, blank=True)
    domain_mismatch_confirmed = models.BooleanField(default=False)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project} → {self.counter_name or self.counter_id}"


class YandexMetrikaSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCESS = "success", "Завершена"
        FAILED = "failed", "Ошибка"

    mapping = models.ForeignKey(
        YandexMetrikaProjectMapping, on_delete=models.CASCADE, related_name="sync_runs"
    )
    report_month = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    error_message = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.mapping} — {self.report_month:%m.%Y}: {self.status}"

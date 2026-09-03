from django.db import models

from apps.projects.models import Project
from apps.yandex.crypto import decrypt_token, encrypt_token


class SerphuntCredential(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    api_key_encrypted = models.BinaryField(editable=False)
    api_key_last_four = models.CharField(max_length=4, blank=True, editable=False)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_api_key(self, value):
        self.api_key_encrypted = encrypt_token(value)
        self.api_key_last_four = value[-4:] if value else ""

    def get_api_key(self):
        return decrypt_token(self.api_key_encrypted)

    def __str__(self):
        return "Общие реквизиты Serphunt"

    @property
    def masked_api_key(self):
        return f"••••{self.api_key_last_four}" if self.api_key_last_four else "Ключ настроен"


class SerphuntProjectMapping(models.Model):
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="serphunt_mapping"
    )
    keywords = models.TextField("Ключевые слова")
    search_engines = models.JSONField("Поисковые системы", default=list)
    region_id = models.PositiveIntegerField("ID региона", default=225)
    region_name = models.CharField("Название региона", max_length=120, blank=True)
    device = models.CharField("Устройство", max_length=16, default="desktop")
    language = models.CharField("Язык", max_length=10, default="ru")
    google_search_depth = models.PositiveSmallIntegerField("Глубина Google", default=100)
    include_subdomains = models.BooleanField("Проверять поддомены", default=False)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def keyword_list(self):
        return list(
            dict.fromkeys(line.strip() for line in self.keywords.splitlines() if line.strip())
        )

    def __str__(self):
        return f"{self.project} → Serphunt"


class SerphuntSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCESS = "success", "Завершена"
        FAILED = "failed", "Ошибка"

    mapping = models.ForeignKey(
        SerphuntProjectMapping, on_delete=models.CASCADE, related_name="sync_runs"
    )
    task_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    loaded_keyword_count = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.mapping}: {self.get_status_display()}"

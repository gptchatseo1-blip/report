import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("projects", "0003_project_position_provider")]
    operations = [
        migrations.CreateModel(
            name="SerphuntCredential",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("api_key_encrypted", models.BinaryField(editable=False)),
                ("api_key_last_four", models.CharField(blank=True, editable=False, max_length=4)),
                ("last_verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="SerphuntProjectMapping",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("keywords", models.TextField(verbose_name="Ключевые слова")),
                (
                    "search_engines",
                    models.JSONField(default=list, verbose_name="Поисковые системы"),
                ),
                ("region_id", models.PositiveIntegerField(default=225, verbose_name="ID региона")),
                (
                    "region_name",
                    models.CharField(blank=True, max_length=120, verbose_name="Название региона"),
                ),
                (
                    "device",
                    models.CharField(default="desktop", max_length=16, verbose_name="Устройство"),
                ),
                ("language", models.CharField(default="ru", max_length=10, verbose_name="Язык")),
                (
                    "google_search_depth",
                    models.PositiveSmallIntegerField(default=100, verbose_name="Глубина Google"),
                ),
                (
                    "include_subdomains",
                    models.BooleanField(default=False, verbose_name="Проверять поддомены"),
                ),
                ("last_successful_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="serphunt_mapping",
                        to="projects.project",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SerphuntSyncRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("task_id", models.CharField(blank=True, max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Выполняется"),
                            ("success", "Завершена"),
                            ("failed", "Ошибка"),
                        ],
                        default="running",
                        max_length=16,
                    ),
                ),
                ("loaded_keyword_count", models.PositiveIntegerField(default=0)),
                ("error_message", models.CharField(blank=True, max_length=500)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "mapping",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sync_runs",
                        to="serphunt.serphuntprojectmapping",
                    ),
                ),
            ],
            options={"ordering": ["-started_at"]},
        ),
    ]

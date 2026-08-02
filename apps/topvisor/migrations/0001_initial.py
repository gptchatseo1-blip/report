import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("projects", "0002_project_top_11_20_mode")]
    operations = [
        migrations.CreateModel(
            name="TopvisorProjectMapping",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "topvisor_project_id",
                    models.CharField(max_length=64, verbose_name="ID проекта Topvisor"),
                ),
                (
                    "topvisor_project_name",
                    models.CharField(blank=True, max_length=255, verbose_name="Проект Topvisor"),
                ),
                (
                    "selected_configurations",
                    models.JSONField(
                        default=list,
                        help_text="ID конкретных поисковой системы и региона",
                        verbose_name="Конфигурации поиска",
                    ),
                ),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="topvisor_mapping",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "verbose_name": "Сопоставление Topvisor",
                "verbose_name_plural": "Сопоставления Topvisor",
            },
        )
    ]

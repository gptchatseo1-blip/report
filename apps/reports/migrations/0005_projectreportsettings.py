import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0002_project_top_11_20_mode"),
        ("reports", "0004_generatedartifact"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectReportSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("values", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="report_settings",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "verbose_name": "Настройки конструктора отчёта",
                "verbose_name_plural": "Настройки конструктора отчётов",
            },
        ),
    ]

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("imports", "0002_importbatch_ranking_depth"),
        ("projects", "0004_project_file_import_provider"),
    ]

    operations = [
        migrations.CreateModel(
            name="FileImportSegment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "search_engine",
                    models.CharField(
                        choices=[("yandex", "Яндекс"), ("google", "Google")], max_length=16
                    ),
                ),
                ("region", models.CharField(max_length=120)),
                ("calculate_visibility", models.BooleanField(default=True)),
                ("last_filename", models.CharField(blank=True, max_length=255)),
                ("imported_at", models.DateTimeField(blank=True, null=True)),
                ("keyword_count", models.PositiveIntegerField(default=0)),
                ("date_count", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("preview", "Ожидает подтверждения"),
                            ("imported", "Импортирован"),
                            ("failed", "Ошибка"),
                        ],
                        default="preview",
                        max_length=16,
                    ),
                ),
                ("error_message", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="file_import_segments",
                        to="projects.project",
                    ),
                ),
            ],
            options={"ordering": ["search_engine", "region", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="fileimportsegment",
            constraint=models.UniqueConstraint(
                fields=("project", "search_engine", "region"), name="unique_file_import_segment"
            ),
        ),
        migrations.AddField(
            model_name="importbatch",
            name="segment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="batches",
                to="imports.fileimportsegment",
            ),
        ),
        migrations.AddField(
            model_name="importbatch",
            name="date_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="importbatch",
            name="kind",
            field=models.CharField(
                choices=[
                    ("topvisor_positions", "Позиции Topvisor"),
                    ("file_positions", "Позиции из файла"),
                ],
                default="topvisor_positions",
                max_length=32,
            ),
        ),
    ]

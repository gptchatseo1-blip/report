import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.imports.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportBatch",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("topvisor_positions", "Позиции Topvisor")],
                        default="topvisor_positions",
                        max_length=32,
                    ),
                ),
                ("original_filename", models.CharField(max_length=255)),
                (
                    "source_file",
                    models.FileField(upload_to=apps.imports.models.import_upload_path),
                ),
                ("file_checksum", models.CharField(max_length=64)),
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
                ("snapshot_date", models.DateField()),
                (
                    "search_engine",
                    models.CharField(
                        choices=[("yandex", "Яндекс"), ("google", "Google")], max_length=16
                    ),
                ),
                ("region", models.CharField(max_length=120)),
                ("total_rows", models.PositiveIntegerField(default=0)),
                ("valid_rows", models.PositiveIntegerField(default=0)),
                ("error_rows", models.PositiveIntegerField(default=0)),
                ("preview_payload", models.JSONField(blank=True, default=list)),
                ("error_summary", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="import_batches",
                        to="projects.project",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="position_imports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Партия импорта",
                "verbose_name_plural": "Партии импорта",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ImportRowError",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("row_number", models.PositiveIntegerField()),
                ("code", models.CharField(max_length=64)),
                ("message", models.CharField(max_length=500)),
                ("raw_values", models.JSONField(blank=True, default=dict)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="row_errors",
                        to="imports.importbatch",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ошибка строки импорта",
                "verbose_name_plural": "Ошибки строк импорта",
                "ordering": ["row_number", "code"],
            },
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.UniqueConstraint(
                fields=(
                    "project",
                    "kind",
                    "file_checksum",
                    "snapshot_date",
                    "search_engine",
                    "region",
                ),
                name="unique_position_import_input",
            ),
        ),
        migrations.AddConstraint(
            model_name="importrowerror",
            constraint=models.UniqueConstraint(
                fields=("batch", "row_number", "code"), name="unique_import_row_error"
            ),
        ),
    ]

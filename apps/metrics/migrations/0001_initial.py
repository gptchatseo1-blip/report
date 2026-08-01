import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("imports", "0001_initial"),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RankingSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("snapshot_date", models.DateField()),
                ("search_engine", models.CharField(max_length=16)),
                ("region", models.CharField(max_length=120)),
                ("tracked_keyword_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "import_batch",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ranking_snapshot",
                        to="imports.importbatch",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ranking_snapshots",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "verbose_name": "Снимок позиций",
                "verbose_name_plural": "Снимки позиций",
                "ordering": ["-snapshot_date", "search_engine", "region"],
            },
        ),
        migrations.CreateModel(
            name="KeywordPosition",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("query", models.CharField(max_length=500)),
                ("normalized_query", models.CharField(max_length=500)),
                ("frequency", models.PositiveIntegerField()),
                ("position_raw", models.CharField(blank=True, max_length=50)),
                ("position_value", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "position_status",
                    models.CharField(
                        choices=[
                            ("ranked", "Есть позиция"),
                            ("beyond_100", "За пределами топ-100"),
                            ("not_found", "Не найден"),
                        ],
                        max_length=16,
                    ),
                ),
                ("group_name", models.CharField(blank=True, max_length=255)),
                ("target_url", models.CharField(blank=True, max_length=2000)),
                ("normalized_target_url", models.CharField(blank=True, max_length=2000)),
                (
                    "ranking_snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="positions",
                        to="metrics.rankingsnapshot",
                    ),
                ),
            ],
            options={
                "verbose_name": "Позиция запроса",
                "verbose_name_plural": "Позиции запросов",
                "ordering": ["position_status", "position_value", "normalized_query"],
            },
        ),
        migrations.AddIndex(
            model_name="rankingsnapshot",
            index=models.Index(
                fields=["project", "snapshot_date", "search_engine", "region"],
                name="ranking_lookup_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="keywordposition",
            index=models.Index(
                fields=["ranking_snapshot", "position_status"], name="position_status_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="keywordposition",
            constraint=models.UniqueConstraint(
                fields=("ranking_snapshot", "normalized_query", "group_name"),
                name="unique_snapshot_query_group",
            ),
        ),
    ]

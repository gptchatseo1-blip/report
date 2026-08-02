import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("metrics", "0002_sourcesnapshot_metricpoint_and_more")]
    operations = [
        migrations.AlterField(
            model_name="rankingsnapshot",
            name="import_batch",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ranking_snapshot",
                to="imports.importbatch",
            ),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="ranking_depth",
            field=models.PositiveSmallIntegerField(default=100),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="depth_raw",
            field=models.CharField(default="100", max_length=100),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="depth_retrieved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="depth_source",
            field=models.CharField(
                choices=[("topvisor_api", "Topvisor API"), ("manual", "Ручной импорт")],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="topvisor_configuration_id",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="visibility",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="visibility_raw",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="response_checksum",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="rankingsnapshot",
            name="retrieved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="rankingsnapshot",
            constraint=models.UniqueConstraint(
                condition=~models.Q(topvisor_configuration_id=""),
                fields=("project", "snapshot_date", "topvisor_configuration_id"),
                name="unique_topvisor_ranking_snapshot",
            ),
        ),
    ]

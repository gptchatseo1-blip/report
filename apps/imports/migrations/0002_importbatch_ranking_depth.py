from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("imports", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="importbatch",
            name="ranking_depth",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (10, "ТОП-10"),
                    (20, "ТОП-20"),
                    (30, "ТОП-30"),
                    (50, "ТОП-50"),
                    (100, "ТОП-100"),
                ],
                default=100,
                verbose_name="Глубина проверки",
            ),
        ),
        migrations.RemoveConstraint(model_name="importbatch", name="unique_position_import_input"),
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
                    "ranking_depth",
                ),
                name="unique_position_import_input",
            ),
        ),
    ]

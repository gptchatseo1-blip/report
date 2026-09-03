from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("metrics", "0007_alter_rankingsnapshot_depth_source"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rankingsnapshot",
            name="import_batch",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=models.RESTRICT,
                related_name="ranking_snapshot",
                to="imports.importbatch",
            ),
        ),
    ]

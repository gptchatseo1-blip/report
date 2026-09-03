from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("metrics", "0006_alter_sourcesnapshot_retrieval_method")]

    operations = [
        migrations.AlterField(
            model_name="rankingsnapshot",
            name="depth_source",
            field=models.CharField(
                choices=[
                    ("topvisor_api", "Topvisor API"),
                    ("serphunt_api", "Serphunt API"),
                    ("manual", "Ручной импорт"),
                ],
                default="manual",
                max_length=16,
            ),
        )
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reports", "0005_projectreportsettings")]

    operations = [
        migrations.AlterField(
            model_name="reportdatasetsnapshot",
            name="version",
            field=models.OneToOneField(
                on_delete=models.deletion.CASCADE,
                related_name="snapshot",
                to="reports.reportversion",
            ),
        )
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("projects", "0002_project_top_11_20_mode")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="position_provider",
            field=models.CharField(
                choices=[("topvisor", "Topvisor"), ("serphunt", "Serphunt")],
                default="topvisor",
                max_length=16,
                verbose_name="Сервис позиций",
            ),
        )
    ]

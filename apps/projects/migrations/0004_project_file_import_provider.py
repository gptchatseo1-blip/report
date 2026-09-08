from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("projects", "0003_project_position_provider")]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="position_provider",
            field=models.CharField(
                choices=[
                    ("topvisor", "Topvisor"),
                    ("serphunt", "Serphunt"),
                    ("file_import", "Импорт из файла"),
                ],
                default="topvisor",
                max_length=16,
                verbose_name="Сервис позиций",
            ),
        )
    ]

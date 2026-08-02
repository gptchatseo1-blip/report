from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("projects", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="project",
            name="top_11_20_mode",
            field=models.CharField(
                choices=[
                    ("auto", "Автоматически"),
                    ("enabled", "Включено"),
                    ("disabled", "Выключено"),
                ],
                default="auto",
                max_length=8,
                verbose_name="Таблица ТОП-11–20",
            ),
        )
    ]

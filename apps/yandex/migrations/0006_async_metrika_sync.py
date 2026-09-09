import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("yandex", "0005_webmaster_mapping_include_iks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="yandexmetrikasyncrun",
            name="force_refresh",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="yandexmetrikasyncrun",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="yandex_metrika_sync_runs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="yandexmetrikasyncrun",
            name="fetched_period_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="yandexmetrikasyncrun",
            name="reused_period_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="yandexmetrikasyncrun",
            name="unavailable_goal_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="yandexmetrikasyncrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "В очереди"),
                    ("running", "Выполняется"),
                    ("success", "Завершена"),
                    ("failed", "Ошибка"),
                ],
                default="running",
                max_length=16,
            ),
        ),
    ]

from django.db import migrations, models


def move_latest_connection(apps, schema_editor):
    TopvisorConnection = apps.get_model("topvisor", "TopvisorConnection")
    TopvisorCredential = apps.get_model("topvisor", "TopvisorCredential")
    connection = (
        TopvisorConnection.objects.filter(last_verified_at__isnull=False)
        .order_by("-last_verified_at", "-updated_at", "-pk")
        .first()
        or TopvisorConnection.objects.order_by("-updated_at", "-pk").first()
    )
    if connection:
        TopvisorCredential.objects.create(
            id=1,
            user_id=connection.user_id,
            api_key_encrypted=connection.api_key_encrypted,
            api_key_last_four=connection.api_key_last_four,
            last_verified_at=connection.last_verified_at,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("topvisor", "0003_topvisorconnection"),
    ]

    operations = [
        migrations.CreateModel(
            name="TopvisorCredential",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "user_id",
                    models.CharField(max_length=255, verbose_name="ID пользователя Topvisor"),
                ),
                ("api_key_encrypted", models.BinaryField()),
                (
                    "api_key_last_four",
                    models.CharField(blank=True, editable=False, max_length=4),
                ),
                ("last_verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Реквизиты Topvisor",
                "verbose_name_plural": "Реквизиты Topvisor",
            },
        ),
        migrations.RunPython(move_latest_connection, migrations.RunPython.noop),
        migrations.DeleteModel(name="TopvisorConnection"),
    ]

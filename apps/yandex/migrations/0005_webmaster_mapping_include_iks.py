from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("yandex", "0004_multiple_webmaster_hosts")]

    operations = [
        migrations.AddField(
            model_name="yandexwebmasterprojectmapping",
            name="include_iks",
            field=models.BooleanField(default=True),
        )
    ]

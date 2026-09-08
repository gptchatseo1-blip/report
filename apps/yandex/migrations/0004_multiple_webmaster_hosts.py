import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0004_project_file_import_provider"),
        ("yandex", "0003_yandexoauthcredential"),
    ]

    operations = [
        migrations.AlterField(
            model_name="yandexwebmasterprojectmapping",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="yandex_webmaster_mappings",
                to="projects.project",
            ),
        ),
        migrations.AddConstraint(
            model_name="yandexwebmasterprojectmapping",
            constraint=models.UniqueConstraint(
                fields=("project", "host_id"), name="unique_project_webmaster_host"
            ),
        ),
        migrations.AlterModelOptions(
            name="yandexwebmasterprojectmapping", options={"ordering": ["id"]}
        ),
    ]

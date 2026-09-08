import django.db.models.deletion
from django.db import migrations, models


def populate_webmaster_source_keys(apps, schema_editor):
    SourceSnapshot = apps.get_model("metrics", "SourceSnapshot")
    for snapshot in SourceSnapshot.objects.filter(
        source="yandex_webmaster", source_key=""
    ).iterator():
        payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
        snapshot.source_key = str(payload.get("host_id") or payload.get("host_url") or "")[:512]
        snapshot.save(update_fields=["source_key"])


class Migration(migrations.Migration):
    dependencies = [
        ("imports", "0003_file_import_segments"),
        ("metrics", "0008_alter_rankingsnapshot_import_batch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rankingsnapshot",
            name="import_batch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="ranking_snapshots",
                to="imports.importbatch",
            ),
        ),
        migrations.AlterField(
            model_name="rankingsnapshot",
            name="depth_source",
            field=models.CharField(
                choices=[
                    ("topvisor_api", "Topvisor API"),
                    ("serphunt_api", "Serphunt API"),
                    ("manual", "Ручной импорт"),
                    ("file_import", "Импорт из файла"),
                ],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="sourcesnapshot", name="unique_project_source_period"
        ),
        migrations.AddField(
            model_name="sourcesnapshot",
            name="source_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.RunPython(populate_webmaster_source_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="sourcesnapshot",
            constraint=models.UniqueConstraint(
                fields=("project", "source", "source_key", "period_start", "period_end"),
                name="unique_project_source_key_period",
            ),
        ),
    ]

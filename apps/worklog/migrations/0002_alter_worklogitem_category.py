from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("worklog", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="worklogitem",
            name="category",
            field=models.ForeignKey(
                on_delete=models.RESTRICT,
                related_name="items",
                to="worklog.workcategory",
                verbose_name="Категория",
            ),
        ),
    ]

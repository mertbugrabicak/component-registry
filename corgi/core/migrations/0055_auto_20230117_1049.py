# Generated by Django 3.2.16 on 2023-01-17 10:49

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0054_auto_20221124_0931"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="component",
            name="data_report",
        ),
        migrations.RemoveField(
            model_name="component",
            name="data_score",
        ),
    ]

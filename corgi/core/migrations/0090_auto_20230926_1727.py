# Generated by Django 3.2.20 on 2023-09-26 17:27

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0089_clean_unshipped_errata"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productcomponentrelation",
            name="build_type",
            field=models.CharField(
                choices=[
                    ("BREW", "Brew"),
                    ("KOJI", "Koji"),
                    ("CENTOS", "Centos"),
                    ("APP_INTERFACE", "App Interface"),
                    ("PNC", "Pnc"),
                    ("PYXIS", "Pyxis"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="softwarebuild",
            name="build_type",
            field=models.CharField(
                choices=[
                    ("BREW", "Brew"),
                    ("KOJI", "Koji"),
                    ("CENTOS", "Centos"),
                    ("APP_INTERFACE", "App Interface"),
                    ("PNC", "Pnc"),
                    ("PYXIS", "Pyxis"),
                ],
                max_length=20,
            ),
        ),
    ]

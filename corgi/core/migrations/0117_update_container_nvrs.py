# Generated by Django 3.2.22 on 2024-02-09 05:57
import logging

from django.db import migrations, models
from django.db.models import F, Value
from django.db.models.functions import Replace

logger = logging.getLogger(__name__)


def update_container_nvrs(apps, schema_editor) -> None:
    Component = apps.get_model("core", "Component")
    for root_container_values in (
        Component.objects.filter(type="OCI", namespace="REDHAT")
        .exclude(software_build=None)
        # This excludes root_containers which were previously updated with this migration therefore
        # it's safe to rerun it more than once
        .exclude(nvr__startswith=F("software_build__name"))
        .values_list("name", "software_build__name")
        .iterator()
    ):
        logger.info(
            f"updating nvr and nevra to include {root_container_values[1]} for containers with "
            f"name: {root_container_values[0]}"
        )
        Component.objects.filter(name=root_container_values[0]).update(
            nvr=Replace(F("nvr"), Value(root_container_values[0]), Value(root_container_values[1])),
            nevra=Replace(
                F("nevra"), Value(root_container_values[0]), Value(root_container_values[1])
            ),
        )


class Migration(migrations.Migration):

    atomic = False
    dependencies = [
        ("core", "0116_accept_set_of_ofuris_to_get_latest_components"),
    ]

    operations = [
        migrations.AlterField(
            model_name="component",
            name="nevra",
            field=models.CharField(default="", max_length=2048),
        ),
        migrations.AlterField(
            model_name="component",
            name="nvr",
            field=models.CharField(default="", max_length=2048),
        ),
        migrations.RunPython(update_container_nvrs),
    ]

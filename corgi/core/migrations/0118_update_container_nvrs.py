# Generated by Django 3.2.22 on 2024-02-09 05:57
import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)


def update_container_nvrs(apps, schema_editor) -> None:
    Component = apps.get_model("core", "Component")
    for root_container_name in (
        Component.objects.filter(type="OCI", namespace="REDHAT")
        .exclude(software_build=None)
        # This excludes root_containers which were previously updated with this migration therefore
        # it's safe to rerun it more than once
        .exclude(nvr__startswith=F("software_build__name"))
        .values_list("name", flat=True)
        .distinct()
    ):
        for c in Component.objects.filter(
            type="OCI", namespace="REDHAT", name=root_container_name
        ).iterator():
            logger.info(f"Calling save for {c.purl}")
            c.save()


class Migration(migrations.Migration):

    atomic = False
    dependencies = [
        ("core", "0117_fix_software_build_names"),
    ]

    operations = [
        migrations.RunPython(update_container_nvrs),
    ]

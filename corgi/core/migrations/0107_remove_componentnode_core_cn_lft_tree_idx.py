# Generated by Django 3.2.20 on 2023-12-01 04:03

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0106_exclude_module_src_from_root_components_condition"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="componentnode",
            name="core_cn_lft_tree_idx",
        ),
    ]

import logging

from celery_singleton import Singleton

from config.celery import app
from corgi.collectors.rhel_compose import RhelCompose
from corgi.core.models import (
    Component,
    ComponentNode,
    ProductComponentRelation,
    ProductStream,
)
from corgi.tasks.brew import save_component, slow_fetch_brew_build
from corgi.tasks.common import RETRY_KWARGS, RETRYABLE_ERRORS

logger = logging.getLogger(__name__)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def save_composes() -> None:
    logger.info("Setting up relations for all streams with composes")
    for stream in ProductStream.objects.exclude(composes__exact={}):
        save_compose.delay(stream.name)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def save_compose(stream_name) -> None:
    logger.info("Called save compose with %s", stream_name)
    ps = ProductStream.objects.get(name=stream_name)
    no_of_relations = 0
    for compose_url, variants in ps.composes.items():
        compose_id, compose_created_date, compose_data = RhelCompose.fetch_compose_data(
            compose_url, variants
        )
        for key in "srpms", "rhel_modules":
            no_of_relations += _create_relations(compose_data, key, compose_id, stream_name)
    logger.info("Created %s new relations for stream %s", no_of_relations, stream_name)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def fetch_compose_build(build_id: int):
    rhel_module_data = RhelCompose.fetch_rhel_module(build_id)
    # Some compose build_ids in the relations table will be for SRPMs, skip those here
    if not rhel_module_data:
        return
    obj, created = Component.objects.get_or_create(
        name=rhel_module_data["meta"]["name"],
        type=Component.Type.RHEL_MODULE,
        arch=rhel_module_data["meta"].get("arch", ""),
        version=rhel_module_data["meta"]["version"],
        release=rhel_module_data["meta"]["release"],
        defaults={
            # This gives us an indication as to which task (this or fetch_brew_build)
            # last processed the module
            "meta_attr": rhel_module_data["analysis_meta"],
        },
    )
    # This should result in a lookup if fetch_brew_build has already processed this module.
    # Likewise if fetch_brew_build processes the module subsequently we should not create
    # a new ComponentNode, instead the same one will be looked up and used as the root node
    node, _ = obj.cnodes.get_or_create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        purl=obj.purl,
        defaults={
            "object_id": obj.pk,
            "obj": obj,
        },
    )
    for c in rhel_module_data.get("components", []):
        # Request fetch of the SRPM build_ids here to ensure software_builds are created and linked
        # to the RPM components. We don't link the SRPM into the tree because some of it's RPMs
        # might not be included in the module
        if "brew_build_id" in c:
            slow_fetch_brew_build.delay(c["brew_build_id"])
        save_component(c, node)


def _create_relations(compose_data, key, compose_id, stream_name) -> int:
    no_of_relations = 0
    if key not in compose_data:
        return no_of_relations
    for build_id in compose_data[key]:
        _, created = ProductComponentRelation.objects.get_or_create(
            external_system_id=compose_id,
            product_ref=stream_name,
            build_id=build_id,
            defaults={"type": ProductComponentRelation.Type.COMPOSE},
        )
        if created:
            no_of_relations += 1
    return no_of_relations


def get_builds_by_compose(compose_names):
    relations_query = (
        ProductComponentRelation.objects.filter(
            external_system_id__in=compose_names,
            type=ProductComponentRelation.Type.COMPOSE,
        )
        .values_list("build_id", flat=True)
        .distinct()
    )
    return _fetch_compose_builds(relations_query)


def get_builds_by_stream(stream_name):
    relations_query = (
        ProductComponentRelation.objects.filter(
            product_ref=stream_name,
            type=ProductComponentRelation.Type.COMPOSE,
        )
        .values_list("build_id", flat=True)
        .distinct()
    )
    return _fetch_compose_builds(relations_query)


def get_all_builds():
    relations_query = (
        ProductComponentRelation.objects.filter(
            type=ProductComponentRelation.Type.COMPOSE,
        )
        .values_list("build_id", flat=True)
        .distinct()
    )
    return _fetch_compose_builds(relations_query)


def _fetch_compose_builds(relations_query):
    build_ids: set[int] = set()
    for build_id in relations_query:
        build_id_int = int(build_id)
        build_ids.add(build_id_int)
        fetch_compose_build.delay(build_id_int)
    return list(build_ids)

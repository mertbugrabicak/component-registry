import re
from typing import Optional

from celery.utils.log import get_task_logger
from celery_singleton import Singleton
from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import dateformat, dateparse, timezone
from django.utils.timezone import make_aware

from config.celery import app
from corgi.collectors.brew import Brew, BrewBuildTypeNotSupported
from corgi.core.models import (
    Component,
    ComponentNode,
    ProductComponentRelation,
    ProductStream,
    SoftwareBuild,
)
from corgi.tasks.common import (
    BUILD_TYPE,
    RETRY_KWARGS,
    RETRYABLE_ERRORS,
    create_relations,
    get_last_success_for_task,
)
from corgi.tasks.errata_tool import slow_load_errata
from corgi.tasks.sca import cpu_software_composition_analysis

logger = get_task_logger(__name__)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def slow_fetch_brew_build(
    build_id: str,
    build_type: str = BUILD_TYPE,
    save_product: bool = True,
    force_process: bool = False,
):
    logger.info("Fetch brew build called with build id: %s", build_id)

    try:
        softwarebuild = SoftwareBuild.objects.get(build_id=build_id, build_type=build_type)
    except SoftwareBuild.DoesNotExist:
        pass
    else:
        if not force_process:
            logger.info("Already processed build_id %s", build_id),
            if save_product:
                logger.info("Only saving product taxonomy for build_id %s", build_id)
                softwarebuild.save_product_taxonomy()
                for related_component in softwarebuild.components.get_queryset():
                    related_component.save_component_taxonomy()
            return
        else:
            logger.info("Fetching brew build with build_id: %s", build_id)

    try:
        component = Brew(build_type).get_component_data(int(build_id))
    except BrewBuildTypeNotSupported as exc:
        logger.warning(str(exc))
        return

    if not component:
        logger.info("No data fetched for build %s from Brew, exiting...", build_id)
        return

    build_meta = component["build_meta"]["build_info"]
    build_meta["corgi_ingest_start_dt"] = dateformat.format(timezone.now(), "Y-m-d H:i:s")
    build_meta["corgi_ingest_status"] = "INPROGRESS"

    completion_time = build_meta.get("completion_time", "")
    if completion_time:
        dt = dateparse.parse_datetime(completion_time.split(".")[0])
        if dt:
            completion_dt = make_aware(dt)
        else:
            logger.info("Could not parse completion_time for build %s", build_id)
            return
    else:
        logger.info("No completion_time for build %s", build_id)
        return

    softwarebuild, created = SoftwareBuild.objects.get_or_create(
        build_id=build_id,
        build_type=build_type,
        defaults={
            "meta_attr": build_meta,
            "name": component["meta"]["name"],
            "source": component["build_meta"]["build_info"]["source"],
        },
        completion_time=completion_dt,
    )

    if not force_process and not created:
        # If another task starts while this task is downloading data this can result in processing
        # the same build twice, let's just bail out here to save cpu
        logger.warning("SoftwareBuild with build_id %s already existed, not reprocessing", build_id)
        return

    if component["type"] == Component.Type.RPM:
        root_node = save_srpm(softwarebuild, component)
    elif component["type"] == Component.Type.CONTAINER_IMAGE:
        root_node = save_container(softwarebuild, component)
    elif component["type"] == Component.Type.RPMMOD:
        root_node = save_module(softwarebuild, component)
    else:
        logger.warning(f"Build {build_id} type is not supported: {component['type']}")
        return

    for child_component in component.get("components", []):
        save_component(child_component, root_node, softwarebuild)

    # We don't call save_product_taxonomy by default to allow async call of slow_load_errata task
    # See CORGI-21
    if save_product:
        softwarebuild.save_product_taxonomy()
        for related_component in softwarebuild.components.get_queryset():
            related_component.save_component_taxonomy()

    # for builds with errata tags set ProductComponentRelation
    # get_component_data always calls _extract_advisory_ids to set tags, but list may be empty
    if not build_meta["errata_tags"]:
        logger.info("no errata tags")
    else:
        if isinstance(build_meta["errata_tags"], str):
            slow_load_errata.delay(build_meta["errata_tags"], force_process=force_process)
        else:
            for e in build_meta["errata_tags"]:
                slow_load_errata.delay(e, force_process=force_process)

    build_ids = component.get("nested_builds", ())
    logger.info("Fetching brew builds for (%s, %s)", build_ids, build_type)
    for b_id in build_ids:
        logger.info("Requesting fetch of nested build: (%s, %s)", b_id, build_type)
        slow_fetch_brew_build.delay(
            b_id, build_type, save_product=save_product, force_process=force_process
        )

    logger.info("Requesting software composition analysis for %s", softwarebuild.pk)
    if settings.SCA_ENABLED:
        cpu_software_composition_analysis.delay(str(softwarebuild.pk), force_process=force_process)

    logger.info("Finished fetching brew build: (%s, %s)", build_id, build_type)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def slow_fetch_modular_build(build_id: str, force_process: bool = False) -> None:
    logger.info("Fetch modular build called with build id: %s", build_id)
    rhel_module_data = Brew.fetch_rhel_module(build_id)
    # Some compose build_ids in the relations table will be for SRPMs, skip those here
    if not rhel_module_data:
        logger.info("No module data fetched for build %s from Brew, exiting...", build_id)
        slow_fetch_brew_build.delay(build_id, force_process=force_process)
        return
    # Note: module builds don't include arch information, only the individual RPMs that make up a
    # module are built for specific architectures.
    # TODO: Merge below with similar logic in save_module() if possible
    meta = rhel_module_data["meta"]
    obj, created = Component.objects.update_or_create(
        type=rhel_module_data["type"],
        name=meta.pop("name"),
        version=meta.pop("version"),
        release=meta.pop("release"),
        arch="noarch",
        defaults={
            # Any remaining meta keys are recorded in meta_attr
            "meta_attr": meta,
            "namespace": Component.Namespace.REDHAT,
        },
    )
    # This should result in a lookup if slow_fetch_brew_build has already processed this module.
    # Likewise if slow_fetch_brew_build processes the module subsequently we should not create
    # a new ComponentNode, instead the same one will be looked up and used as the root node
    node = save_node(ComponentNode.ComponentNodeType.SOURCE, None, obj)

    for c in rhel_module_data.get("components", []):
        # Request fetch of the SRPM build_ids here to ensure software_builds are created and linked
        # to the RPM components. We don't link the SRPM into the tree because some of it's RPMs
        # might not be included in the module
        if "brew_build_id" in c:
            slow_fetch_brew_build.delay(c["brew_build_id"])
        save_component(c, node)
    slow_fetch_brew_build.delay(build_id, force_process=force_process)
    logger.info("Finished fetching modular build: %s", build_id)


def find_package_file_name(sources: list[str]) -> str:
    """Find a packageFileName for a manifest using a list of source filenames from a build system"""
    for source in sources:
        # Use first matching source value that looks like a package
        match = re.search(r"\.(?:rpm|tar|tgz|zip)", source)
        if match:
            return source
    return ""  # If sources was an empty list, or none of the filenames matched


def save_component(
    component: dict, parent: ComponentNode, softwarebuild: Optional[SoftwareBuild] = None
):
    logger.debug("Called save component with component %s", component)
    component_type = component.pop("type").upper()
    meta = component.get("meta", {})

    node_type = ComponentNode.ComponentNodeType.PROVIDES
    if meta.pop("dev", False):
        node_type = ComponentNode.ComponentNodeType.PROVIDES_DEV

    component_version = meta.pop("version", "")
    if component_type in ("GO-PACKAGE", "GOMOD"):
        component_type = Component.Type.GOLANG

    elif component_type == "PIP":
        component_type = "PYPI"

    elif component_type not in Component.Type.values:
        logger.warning("Tried to create component with invalid component_type: %s", component_type)
        return

    # Only save softwarebuild for RPM where they are direct children of SRPMs
    # This avoids the situation where only the latest build fetched has the softarebuild associated
    # For example if we were processing a container image with embedded rpms this could be set to
    # the container build id, whereas we want it also to reflect the build id of the RPM build
    if not (softwarebuild and parent.obj is not None and parent.obj.is_srpm()):
        softwarebuild = None

    related_url = meta.pop("url", "")
    if not related_url:
        # Only RPMs have a URL header, containers might have below
        related_url = meta.get("repository_url", "")
    if not related_url:
        # Handle case when key is present but value is None
        related_url = ""

    license_declared_raw = meta.pop("license", "")
    # We can't build a purl before the component is saved,
    # so we can't handle an IntegrityError (duplicate purl) here like we do in the SCA task
    # But that's OK - this task shouldn't ever raise an IntegrityError
    # The "original" component should be created here as part of normal ingestion
    # The duplicate components (new name, same purl) are created by Syft / the SCA task later
    obj, _ = Component.objects.update_or_create(
        type=component_type,
        name=meta.pop("name", ""),
        version=component_version,
        release=meta.pop("release", ""),
        arch=meta.pop("arch", "noarch"),
        defaults={
            "description": meta.pop("description", ""),
            "filename": find_package_file_name(meta.pop("source_files", [])),
            "namespace": Component.Namespace.REDHAT
            if component_type == Component.Type.RPM
            else Component.Namespace.UPSTREAM,
            "related_url": related_url,
            "software_build": softwarebuild,
        },
    )

    set_license_declared_safely(obj, license_declared_raw)

    # Usually component_meta is an empty dict by the time we get here, but if it's not, and we have
    # new keys, add them to the existing meta_attr. Only call save if something has been added
    if meta:
        obj.meta_attr = obj.meta_attr | meta
        obj.save()

    node = save_node(node_type, parent, obj)
    recurse_components(component, node)


def save_srpm(softwarebuild: SoftwareBuild, build_data: dict) -> ComponentNode:
    related_url = build_data["meta"].pop("url", "")
    if not related_url:
        # Handle case when key is present but value is None
        related_url = ""

    obj, created = Component.objects.update_or_create(
        type=build_data["type"],
        name=build_data["meta"].get("name"),
        version=build_data["meta"].get("version", ""),
        release=build_data["meta"].get("release", ""),
        arch=build_data["meta"].get("arch", "noarch"),
        defaults={
            "description": build_data["meta"].get("description", ""),
            "license_declared_raw": build_data["meta"].get("license", ""),
            "meta_attr": build_data["meta"],
            "namespace": Component.Namespace.REDHAT,
            "related_url": related_url,
            "software_build": softwarebuild,
        },
    )
    node = save_node(ComponentNode.ComponentNodeType.SOURCE, None, obj)
    if related_url:
        new_upstream, created = Component.objects.update_or_create(
            type=build_data["type"],
            name=build_data["meta"].get("name"),
            version=build_data["meta"].get("version", ""),
            release="",
            arch="noarch",
            defaults={
                "description": build_data["meta"].get("description", ""),
                "filename": find_package_file_name(build_data["meta"].get("source_files", [])),
                "license_declared_raw": build_data["meta"].get("license", ""),
                "meta_attr": build_data["meta"],
                "namespace": Component.Namespace.UPSTREAM,
                "related_url": related_url,
            },
        )
        save_node(ComponentNode.ComponentNodeType.SOURCE, node, new_upstream)
    return node


def process_image_components(image):
    builds_to_fetch = set()
    if "rpm_components" in image:
        for rpm in image["rpm_components"]:
            builds_to_fetch.add(rpm["brew_build_id"])
        # TODO save the list of rpms by image to the container meta for reconcilation.
    return builds_to_fetch


def set_license_declared_safely(obj: Component, license_declared_raw: str) -> None:
    """Save a declared license onto a Component, without erasing any existing value"""
    if license_declared_raw and license_declared_raw != obj.license_declared_raw:
        # Any non-empty license here should be reported
        # We only rely on OpenLCS if we don't know the license_declared
        # But we can't set license_declared in update_or_create
        # If the license in the metadata is an empty string and we are reprocessing,
        # we might erase the license that OpenLCS provided
        # They cannot erase any licenses we set ourselves (when the field is not empty)
        # API endpoint blocks this (400 Bad Request)
        obj.license_declared_raw = license_declared_raw
        obj.save()


def save_container(softwarebuild: SoftwareBuild, build_data: dict) -> ComponentNode:
    license_declared_raw = build_data["meta"].pop("license", "")
    related_url = build_data["meta"].get("repository_url", "")
    if not related_url:
        # Handle case when key is present but value is None
        related_url = ""

    obj, created = Component.objects.update_or_create(
        type=build_data["type"],
        name=build_data["meta"].pop("name"),
        version=build_data["meta"].pop("version"),
        release=build_data["meta"].pop("release"),
        arch="noarch",
        defaults={
            "description": build_data["meta"].pop("description", ""),
            "filename": build_data["meta"].pop("filename", ""),
            "meta_attr": build_data["meta"],
            "namespace": Component.Namespace.REDHAT,
            "related_url": related_url,
            "software_build": softwarebuild,
        },
    )

    set_license_declared_safely(obj, license_declared_raw)
    root_node = save_node(ComponentNode.ComponentNodeType.SOURCE, None, obj)

    if "upstream_go_modules" in build_data["meta"]:
        for module in build_data["meta"]["upstream_go_modules"]:
            new_upstream, created = Component.objects.get_or_create(
                type=Component.Type.GOLANG,
                name=module,
                # the upstream commit is included in the dist-git commit history, but is not
                # exposed anywhere in the brew data that I can find
                version="",
                release="",
                arch="noarch",
                defaults={
                    "mets_attr": {"go_component_type": "gomod"},
                    "namespace": Component.Namespace.UPSTREAM,
                },
            )
            save_node(ComponentNode.ComponentNodeType.SOURCE, root_node, new_upstream)

    if "image_components" in build_data:
        for image in build_data["image_components"]:
            license_declared_raw = image["meta"].pop("license", "")

            obj, created = Component.objects.update_or_create(
                type=image["type"],
                name=image["meta"].pop("name"),
                version=image["meta"].pop("version"),
                release=image["meta"].pop("release"),
                arch=image["meta"].pop("arch"),
                defaults={
                    "description": image["meta"].pop("description", ""),
                    "filename": image["meta"].pop("filename", ""),
                    "meta_attr": image["meta"],
                    "namespace": Component.Namespace.REDHAT,
                    "software_build": softwarebuild,
                },
            )

            set_license_declared_safely(obj, license_declared_raw)
            # Based on a conversation with the container factory team,
            # almost all image components are build-time dependencies in a multi-stage build
            # and are discarded / do not end up in the final image.
            # The only exceptions are image components from the base layer (ie UBI)
            # So we should probably still use PROVIDES here, and not PROVIDES_DEV
            # Unless we can distinguish between these two types of components
            # using some other Brew metadata
            image_arch_node = save_node(ComponentNode.ComponentNodeType.PROVIDES, root_node, obj)

            if "rpm_components" in image:
                for rpm in image["rpm_components"]:
                    save_component(rpm, image_arch_node)
                    # SRPMs are loaded using nested_builds

    if "sources" in build_data:
        for source in build_data["sources"]:
            component_name = source["meta"].pop("name")
            related_url = source["meta"].pop("url", "")
            if not related_url:
                # Handle case when key is present but value is None
                related_url = ""
                if component_name.startswith("github.com/"):
                    related_url = f"https://{component_name}"
            if "openshift-priv" in related_url:
                # Component name is something like github.com/openshift-priv/cluster-api
                # The public repo we want is just github.com/openshift/cluster-api
                related_url = related_url.replace("openshift-priv", "openshift")

            if source["type"] == Component.Type.GOLANG:
                # Assume upstream container sources are always go modules, never go-packages
                source["meta"]["go_component_type"] = "gomod"
            new_upstream, created = Component.objects.update_or_create(
                type=source["type"],
                name=component_name,
                version=source["meta"].pop("version"),
                release="",
                arch="noarch",
                defaults={
                    "meta_attr": source["meta"],
                    "namespace": Component.Namespace.UPSTREAM,
                    "related_url": related_url,
                },
            )
            upstream_node = save_node(
                ComponentNode.ComponentNodeType.SOURCE, root_node, new_upstream
            )

            # Collect the Cachito dependencies
            recurse_components(source, upstream_node)
    return root_node


def recurse_components(component: dict, parent: ComponentNode):
    if not parent:
        logger.warning(f"Failed to create ComponentNode for component: {component}")
    else:
        if "components" in component:
            for child in component["components"]:
                save_component(child, parent)


def save_module(softwarebuild, build_data) -> ComponentNode:
    """Upstreams are not created because modules have no related source code. They are a
    collection of RPMs from other SRPMS. The upstreams can be looked up from all the RPM children.
    No child components are created here because we don't have enough data in Brew to determine
    the relationships. We create the relationships using data from RHEL_COMPOSE, or RPM repository
    See CORGI-200, and CORGI-163"""
    meta_attr = build_data["meta"]["meta_attr"]
    obj, created = Component.objects.update_or_create(
        type=build_data["type"],
        name=build_data["meta"]["name"],
        version=build_data["meta"].get("version", ""),
        release=build_data["meta"].get("release", ""),
        arch=build_data["meta"].get("arch", "noarch"),
        defaults={
            "description": build_data["meta"].get("description", ""),
            "license_declared_raw": build_data["meta"].get("license", ""),
            "meta_attr": meta_attr,
            "namespace": Component.Namespace.REDHAT,
            "software_build": softwarebuild,
        },
    )
    node = save_node(ComponentNode.ComponentNodeType.SOURCE, None, obj)

    return node


def save_node(
    node_type: str, parent: Optional[ComponentNode], related_component: Component
) -> ComponentNode:
    """Helper function that wraps ComponentNode creation"""
    node, _ = ComponentNode.objects.get_or_create(
        type=node_type,
        parent=parent,
        purl=related_component.purl,
        defaults={"obj": related_component},
    )
    return node


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def load_brew_tags() -> None:
    for ps in ProductStream.objects.get_queryset():
        build_type = BUILD_TYPE
        brew = Brew(BUILD_TYPE)
        # This should really be a property in Product Definitions
        if settings.COMMUNITY_MODE_ENABLED and ps.name == "openstack-rdo":
            brew = Brew(SoftwareBuild.Type.CENTOS)
            build_type = SoftwareBuild.Type.CENTOS
        for brew_tag, inherit in ps.brew_tags.items():
            # Always load all builds in tag when saving relations
            builds = brew.get_builds_with_tag(brew_tag, inherit=inherit, latest=False)
            no_of_created = create_relations(
                builds, build_type, brew_tag, ps.name, ProductComponentRelation.Type.BREW_TAG
            )
            logger.info("Saving %s new builds for %s", no_of_created, brew_tag)


def fetch_modular_builds(relations_query: QuerySet, force_process: bool = False) -> None:
    for build_id in relations_query:
        slow_fetch_modular_build.delay(build_id, force_process=force_process)


def fetch_unprocessed_relations(
    relation_type: ProductComponentRelation.Type,
    created_since: Optional[timezone.datetime] = None,
    force_process: bool = False,
) -> int:
    query = Q(type=relation_type)
    if created_since:
        query &= Q(created_at__gte=created_since)
    relations_query = (
        ProductComponentRelation.objects.filter(query)
        .values_list("build_id", "build_type")
        .distinct()
        .using("read_only")
    )
    logger.info(f"Processing relations of type {relation_type}")
    processed_builds = 0
    for build_id, build_type in relations_query.iterator():
        if not build_id:
            # build_id defaults to "" and int() will fail in this case
            continue
        if (
            not SoftwareBuild.objects.filter(build_id=build_id, build_type=build_type)
            .using("read_only")
            .exists()
        ):
            logger.info("Processing %s relation build with id: %s", relation_type, build_id)
            if build_type == SoftwareBuild.Type.CENTOS:
                # This skips use of the Collector models for builds in the CENTOS koji instance
                # It was done to avoid updating the collector models not to use build_id as
                # a primary key. It's possible because the only product stream (openstack-rdo)
                # stored in CENTOS koji doesn't use modules
                slow_fetch_brew_build.delay(
                    build_id, SoftwareBuild.Type.CENTOS, force_process=force_process
                )
                processed_builds += 1
                continue
            slow_fetch_modular_build.delay(build_id, force_process=force_process)
            processed_builds += 1
    return processed_builds


@app.task(
    base=Singleton,
    autorety_for=RETRYABLE_ERRORS,
    retry_kwargs=RETRY_KWARGS,
    soft_time_limit=settings.CELERY_LONGEST_SOFT_TIME_LIMIT,
)
def fetch_unprocessed_brew_tag_relations(
    force_process: bool = False, days_created_since: int = 0
) -> int:
    if days_created_since:
        created_dt = timezone.now() - timezone.timedelta(days=days_created_since)
    else:
        created_dt = get_last_success_for_task(
            "corgi.tasks.brew.fetch_unprocessed_brew_tag_relations"
        )
    return fetch_unprocessed_relations(
        ProductComponentRelation.Type.BREW_TAG,
        force_process=force_process,
        created_since=created_dt,
    )

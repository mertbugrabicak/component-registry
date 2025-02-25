import json
import logging
import uuid
from json import JSONDecodeError

import jsonschema
import pytest
from django.conf import settings
from django_celery_results.models import TaskResult

from corgi.core.files import ComponentManifestFile, ProductManifestFile
from corgi.core.fixups import cpe_lookup
from corgi.core.models import (
    Component,
    ComponentNode,
    ProductComponentRelation,
    ProductNode,
)
from corgi.tasks.manifest import same_contents
from corgi.web.templatetags.base_extras import provided_relationship

from .conftest import setup_product
from .factories import (
    BinaryRpmComponentFactory,
    ComponentFactory,
    ContainerImageComponentFactory,
    ProductComponentRelationFactory,
    ProductStreamFactory,
    ProductStreamNodeFactory,
    ProductVariantFactory,
    ProductVariantNodeFactory,
    SoftwareBuildFactory,
    SrpmComponentFactory,
    UpstreamComponentFactory,
)

logger = logging.getLogger()
pytestmark = [
    pytest.mark.unit,
    pytest.mark.django_db(databases=("default", "read_only"), transaction=True),
]

# From https://raw.githubusercontent.com/spdx/spdx-spec/development/
# v2.2.2/schemas/spdx-schema.json
SCHEMA_FILE = settings.BASE_DIR / "corgi/web/static/spdx-22-schema.json"


def test_escapejs_on_copyright():
    # actual copyright_text from snappy rpm
    c = ComponentFactory(
        copyright_text="(c) A AEZaO5, (c) ^P&#x27;,AE^UG.2oDS&#x27;x c v KDC(r)xOE@5wZ&#x27;^NyH+c"
        "@^AP6| bV, (c) /axove+xose/7,-1,0,B/frameset&amp;F axuntanza&amp;1,,3 http"
        "://biblio.cesga.es:81/search, (c) /axove+xose/7,-1,0,B/frameset&amp;F axun"
        "tanza&amp;3,,3 http://db.zaq.ne.jp/asp/bbs/jttk_baasc506_1/article/36 http:"
        "//db.zaq.ne.jp/asp/bbs/jttk_baasc506_1/article/37, (c) Ei El, (c) H2 (c), ("
        "c) I UuE, (c) OOUA UuUS1a OEviy, Copyright 2005 and onwards Google Inc., Co"
        "pyright 2005 Google Inc., Copyright 2008 Google Inc., Copyright 2011 Google"
        " Inc., Copyright 2011, Google Inc., Copyright 2011 Martin Gieseking &lt;mar"
        "tin.gieseking@uos.de&gt;, Copyright 2013 Steinar H. Gunderson, Copyright 20"
        "19 Google Inc., copyright by Cornell University, Copyright Depository of El"
        "ectronic Materials, Copyright Issues Marybeth Peters, (c) ^P u E2OroCIyV ^T"
        " C.au, (c) UiL (c)"
    )
    try:
        json.dumps(ComponentManifestFile(c).render_content())
    except JSONDecodeError as e:
        assert (
            False
        ), f"JSONDecodeError thrown by component with special chars in copyright_text {e}"

    c = ComponentFactory(copyright_text="©, 🄯, ®, ™")
    try:
        json.dumps(ComponentManifestFile(c).render_content())
    except JSONDecodeError as e:
        assert (
            False
        ), f"JSONDecodeError thrown by component with special chars in copyright_text {e}"


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_manifests_exclude_source_container(stored_proc):
    """Test that container sources are excluded packages"""
    containers, stream, _ = setup_products_and_rpm_in_containers()
    assert len(containers) == 3

    manifest = ProductManifestFile(stream).render_content()
    components = manifest["packages"]
    # Two containers, one RPM, and a product are included
    assert len(components) == 4
    # The source container is excluded
    for component in components:
        assert not component["name"].endswith("-container-source")


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_manifests_exclude_bad_golang_components(stored_proc):
    """Test that Golang components with names like ../ are excluded packages"""
    # Remove below when CORGI-428 is resolved
    containers, stream, _ = setup_products_and_rpm_in_containers()
    assert len(containers) == 3

    bad_golang = ComponentFactory(type=Component.Type.GOLANG, name="./api-")
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.PROVIDES,
        parent=containers[0].cnodes.first(),
        obj=bad_golang,
    )
    # Link the bad_golang component to its parent container
    containers[0].save_component_taxonomy()
    assert containers[0].provides.filter(name=bad_golang.name).exists()

    manifest = ProductManifestFile(stream).render_content()
    components = manifest["packages"]
    # Two containers, one RPM, and a product are included
    assert len(components) == 4, components
    # The golang component is excluded
    for component in components:
        assert not component["name"] == bad_golang.name


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_stream_manifest_backslash(stored_proc):
    """Test that a tailing backslash in a purl doesn't break rendering"""
    stream_node = ProductStreamNodeFactory()
    stream = stream_node.obj
    sb = SoftwareBuildFactory()
    component = SrpmComponentFactory(version="2.8.0 \\", software_build=sb)
    component.productstreams.add(stream)

    try:
        json.dumps(ProductManifestFile(stream).render_content())
    except JSONDecodeError:
        assert False


def test_component_manifest_backslash():
    """Test that a backslash in a version doesn't break rendering via download_url or related_url"""
    component = ComponentFactory(
        version="\\9.0.6.v20130930",
        type=Component.Type.MAVEN,
        name="org.eclipse.jetty/jetty-webapp",
    )
    assert component.download_url
    try:
        json.dumps(ComponentManifestFile(component).render_content())
    except JSONDecodeError:
        assert False


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_slim_rpm_in_containers_manifest(stored_proc):
    containers, stream, rpm_in_container = setup_products_and_rpm_in_containers()

    test_cpe = "cpe:/a:redhat:test:1"
    variant = stream.productvariants.get()
    variant.cpe = test_cpe
    variant.save()
    assert test_cpe in stream.cpes

    # Two components linked to this product
    # plus a source container which is shown in API but not in manifests
    released_components = stream.components.manifest_components(ofuri=stream.ofuri)
    num_components = len(released_components)
    assert num_components == 2, released_components

    provided = set()
    # Each released component has 1 provided component each
    for released_component in released_components:
        provided_components = released_component.get_provides_pks()
        assert len(provided_components) == 1
        provided.update(provided_components)

    # Here we are checking that all the released components share a single provided component
    num_provided = len(provided)
    assert num_provided == 1

    distinct_provides = stream.provides_queryset
    assert len(distinct_provides) == num_provided

    document_uuid = f"SPDXRef-{uuid.uuid4()}"

    manifest = ProductManifestFile(stream).render_content(document_uuid=document_uuid)

    # One package for each component
    # One (and only one) package for each distinct provided
    # Plus one package for the stream itself
    assert len(manifest["packages"]) == num_components + num_provided + 1

    # CPE for each root component attached to a product should be included in the manifest
    container_pks = [str(c.pk) for c in containers]
    for package in manifest["packages"]:
        pk = package["SPDXID"][len("SPXRef-") + 1 :]
        if pk in container_pks:
            found_cpe = False
            for ref in package["externalRefs"]:
                if ref["referenceType"] == "cpe22Type":
                    # The CPE matches the one returned by setup_products_and_rpm_in_containers()
                    assert ref["referenceLocator"] == test_cpe
                    found_cpe = True
                    break
            # There was at least one CPE
            assert found_cpe

    # For each component, one "component is package of product" relationship
    # For each provided in component, one "provided contained by component"
    # (only one unique provided component, but two relationships for two parent components)
    # Plus one "document describes product" relationship for the whole document at the end
    assert len(manifest["relationships"]) == num_components + (num_provided + 1) + 1

    provided_uuid = provided.pop()
    component = containers[0]
    # Manifest packages are ordered by UUID, so make sure we assert on the right container
    if containers[0].uuid > containers[1].uuid:
        component = containers[1]

    document_describes_product = {
        "relatedSpdxElement": document_uuid,
        "relationshipType": "DESCRIBES",
        "spdxElementId": "SPDXRef-DOCUMENT",
    }

    component_is_package_of_product = {
        "relatedSpdxElement": document_uuid,
        "relationshipType": "PACKAGE_OF",
        "spdxElementId": f"SPDXRef-{component.uuid}",
    }

    provided_contained_by_component = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "CONTAINED_BY",
        "spdxElementId": f"SPDXRef-{provided_uuid}",
    }

    assert manifest["relationships"][0] == provided_contained_by_component
    assert manifest["relationships"][1] == component_is_package_of_product
    assert manifest["relationships"][-1] == document_describes_product


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_product_manifest_excludes_unreleased_components(stored_proc):
    """Test that manifests for products don't include unreleased components"""
    component, stream, provided, dev_provided = setup_products_and_components_provides(
        released=False
    )

    manifest = ProductManifestFile(stream).render_content()

    # No released components linked to this product
    num_components = len(stream.components.manifest_components(ofuri=stream.get_ofuri()))
    assert num_components == 0

    num_provided = len(stream.provides_queryset)
    assert num_provided == 0

    # Manifest contains info for no components, no provides, and only the product itself
    assert len(manifest["packages"]) == 1

    # Only "component" is actually the product
    product_data = manifest["packages"][0]

    document_uuid = product_data["SPDXID"]
    assert product_data["name"] == stream.external_name
    assert product_data["externalRefs"][0]["referenceLocator"] == "cpe:/o:redhat:enterprise_linux:8"

    # Only one "document describes product" relationship for the whole document at the end
    assert len(manifest["relationships"]) == 1

    assert manifest["relationships"][0] == {
        "relatedSpdxElement": document_uuid,
        "relationshipType": "DESCRIBES",
        "spdxElementId": "SPDXRef-DOCUMENT",
    }


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_product_manifest_excludes_internal_components(stored_proc):
    """Test that manifests for products don't include unreleased components"""
    component, stream, provided, dev_provided = setup_products_and_components_provides(
        internal_component=True
    )

    manifest = ProductManifestFile(stream).render_content()

    # This is the root rpm src component
    num_components = len(stream.components.manifest_components(ofuri=stream.ofuri))
    assert num_components == 1

    num_provided = len(stream.provides_queryset)
    assert num_provided == 1

    package_data = manifest["packages"]
    for package in package_data:
        assert "redhat.com/" not in package["name"]


def test_manifest_no_duplicate_released_components():
    """Test that the released components queryset
    doesn't give duplicate results in manifests"""
    component, stream, _, _ = setup_products_and_components_provides()
    # Add another Errata relation type for the same build (one already created in
    # setup_product_and_components_provides)
    ProductComponentRelationFactory(
        software_build=component.software_build,
        build_id=component.software_build.build_id,
        build_type=component.software_build.build_type,
        type=ProductComponentRelation.Type.ERRATA,
    )

    unique_components = set()

    for purl in stream.components.manifest_components(ofuri=stream.ofuri).values_list(
        "purl", flat=True
    ):
        assert purl not in unique_components
        unique_components.add(purl)


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_manifest_cpes_from_variants(stored_proc):
    # setup_products_and_components_provides adds a variant with a cpe
    # verify that is the only cpe shown when stream.et_product_versions is set
    stream, _ = setup_product()
    assert stream.productvariants.exists()
    assert len(stream.cpes) == 1

    # Make sure the stream doesn't exist in cpe_lookup
    assert not cpe_lookup(stream.name)

    manifest = ProductManifestFile(stream).render_content()
    product_data = manifest["packages"][-1]
    cpes_in_manifest = [ref["referenceLocator"] for ref in product_data["externalRefs"]]
    assert len(cpes_in_manifest) == 1
    assert cpes_in_manifest[0] == stream.cpes[0]


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_manifest_cpes_from_cpe_lookup(stored_proc):

    hardcoded_cpes = cpe_lookup("rhel-8.8.0")
    stream, _ = setup_product(stream_name="rhel-8.8.0")
    assert stream.productvariants.exists()
    assert hardcoded_cpes

    manifest = ProductManifestFile(stream).render_content()
    product_data = manifest["packages"][-1]
    cpes_in_manifest = set(ref["referenceLocator"] for ref in product_data["externalRefs"])
    assert hardcoded_cpes == cpes_in_manifest


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_manifest_cpes_from_patterns_and_brew_tags(stored_proc):
    stream, variant = setup_product(variant_node_type=ProductNode.ProductNodeType.INFERRED)
    assert not cpe_lookup(stream.name)
    test_cpe = "cpe:/a:redhat:test:1"
    stream.cpes_matching_patterns = [test_cpe]
    stream.save()
    assert stream.cpes == (
        "cpe:/a:redhat:test:1",
        "cpe:/o:redhat:enterprise_linux:8",
    )

    manifest = ProductManifestFile(stream).render_content()
    product_data = manifest["packages"][-1]
    cpes_in_manifest = set(ref["referenceLocator"] for ref in product_data["externalRefs"])
    assert {test_cpe, variant.cpe} == cpes_in_manifest


def _validate_schema(content: str):
    """Raise an exception if content for SPDX file is not valid JSON / SPDX"""
    # The manifest template must use Django's escapejs filter,
    # to generate valid JSON and escape quotes + newlines
    # But this may output ugly Unicode like "\u000A",
    # so we convert from JSON back to JSON to get "\n" instead
    content = json.loads(content)
    with open(SCHEMA_FILE, "r") as schema_file:
        schema = json.load(schema_file)
    jsonschema.validate(content, schema)
    return content


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_product_manifest_properties(stored_proc):
    """Test that all models inheriting from ProductModel have a .manifest property
    And that it generates valid JSON."""
    component, stream, provided, dev_provided = setup_products_and_components_provides()

    manifest = ProductManifestFile(stream).render_content()

    # One component linked to this product
    num_components = len(stream.components.manifest_components(ofuri=stream.ofuri))
    assert num_components == 1

    num_provided = len(stream.provides_queryset)
    assert num_provided == 2

    # Manifest contains info for all components, their provides, and the product itself
    assert len(manifest["packages"]) == num_components + num_provided + 1

    # Last "component" is actually the product
    component_data = manifest["packages"][0]
    product_data = manifest["packages"][-1]

    # UUID, CPE, and PURL for each root component attached to product should be included in manifest
    assert component_data["SPDXID"] == f"SPDXRef-{component.uuid}"
    assert component_data["name"] == component.name
    assert component_data["packageFileName"] == f"{component.nevra}.rpm"
    assert component_data["externalRefs"][0]["referenceLocator"] == component.purl
    assert (
        component_data["externalRefs"][-1]["referenceLocator"]
        == stream.productvariants.values_list("cpe", flat=True).get()
    )

    document_uuid = product_data["SPDXID"]
    assert product_data["name"] == stream.external_name

    document_describes_product = {
        "relatedSpdxElement": f"{document_uuid}",
        "relationshipType": "DESCRIBES",
        "spdxElementId": "SPDXRef-DOCUMENT",
    }

    component_is_package_of_product = {
        "relatedSpdxElement": f"{document_uuid}",
        "relationshipType": "PACKAGE_OF",
        "spdxElementId": f"SPDXRef-{component.uuid}",
    }

    provided_contained_by_component = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "CONTAINED_BY",
        "spdxElementId": f"SPDXRef-{provided.uuid}",
    }

    dev_provided_dependency_of_component = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "DEV_DEPENDENCY_OF",
        "spdxElementId": f"SPDXRef-{dev_provided.uuid}",
    }

    # For each provided in component, one "provided contained by component"
    # For each provided in component, one "provided contains none" indicating a leaf node
    # For each component, one "component is package of product" relationship
    # Plus one "document describes product" relationship for the whole document at the end
    assert len(manifest["relationships"]) == num_components + num_provided + 1

    if dev_provided.uuid < provided.uuid:
        dev_provided_index = 0
        provided_index = 1
    else:
        provided_index = 0
        dev_provided_index = 1

    assert manifest["relationships"][provided_index] == provided_contained_by_component
    assert manifest["relationships"][dev_provided_index] == dev_provided_dependency_of_component
    assert manifest["relationships"][num_provided] == component_is_package_of_product
    assert manifest["relationships"][-1] == document_describes_product


@pytest.mark.django_db(databases=("default", "read_only"), transaction=True)
def test_no_duplicates_in_manifest_with_upstream(stored_proc):
    stream, component, other_component, upstream = setup_products_and_components_upstreams()

    assert component.upstreams.get(pk=upstream.uuid)
    assert other_component.upstreams.get(pk=upstream.uuid)

    # 1 (product) + 2 (root components) + 1 (upstream)
    root_components = stream.components.manifest_components(ofuri=stream.ofuri)
    assert len(root_components) == 2
    assert component in root_components
    assert other_component in root_components

    upstream_components = stream.upstreams_queryset
    assert len(upstream_components) == 1
    assert upstream_components.first() == upstream

    manifest = ProductManifestFile(stream).render_content()
    assert len(manifest["packages"]) == 4


license_expressions = [
    (
        "0BSD",
        "0BSD",
        {},
    ),
    # I'm surprised 0BSD+ is not recognized as an alias to 0BSD or later
    (
        "0BSD+",
        "LicenseRef-0",
        {"0BSD+": "0"},
    ),
    (
        "0BSD+ or 0BSD+",
        "LicenseRef-0 OR LicenseRef-0",
        {"0BSD+": "0"},
    ),
    (
        "BSD-3-Clause or GPLv3+",
        "BSD-3-Clause OR LicenseRef-0",
        {"GPLv3+": "0"},
    ),
    (
        "BSD-3-Clause or (GPLv3+ or LGPLv3+)",
        "BSD-3-Clause OR (LicenseRef-0 OR LicenseRef-1)",
        {
            "GPLv3+": "0",
            "LGPLv3+": "1",
        },
    ),
    (
        "BSD-3-Clause with exceptions",
        "LicenseRef-0",
        {"BSD-3-Clause with exceptions": "0"},
    ),
    (
        "BSD-3-Clause or (GPLv3+ with exceptions and LGPLv3+) and Public Domain",
        "LicenseRef-0",
        {"BSD-3-Clause or (GPLv3+ with exceptions and LGPLv3+) and Public Domain": "0"},
    ),
    # Actual license declared data examples
    (
        "(MPLv1.1 or LGPLv3+) and LGPLv3 and LGPLv2+ and BSD and (MPLv1.1 or GPLv2 or LGPLv2 or "
        "Netscape) and Public Domain and ASL 2.0 and MPLv2.0 and CC0",
        "(LicenseRef-MPLv1.1 OR LicenseRef-0) AND LicenseRef-LGPLv3 AND LicenseRef-1 AND "
        "LicenseRef-BSD AND (LicenseRef-MPLv1.1 OR LicenseRef-GPLv2 OR LicenseRef-LGPLv2 OR "
        "LicenseRef-Netscape) AND LicenseRef-2 AND LicenseRef-3 AND LicenseRef-MPLv2.0 AND "
        "LicenseRef-CC0",
        {
            "MPLv1.1": "MPLv1.1",
            "LGPLv3+": "0",
            "LGPLv3": "LGPLv3",
            "LGPLv2+": "1",
            "BSD": "BSD",
            "GPLv2": "GPLv2",
            "LGPLv2": "LGPLv2",
            "Netscape": "Netscape",
            "Public Domain": "2",
            "ASL 2.0": "3",
            "MPLv2.0": "MPLv2.0",
            "CC0": "CC0",
        },
    ),
    (
        "GPLv2 and Redistributable, no modification permitted",
        "LicenseRef-0",
        {"GPLv2 and Redistributable, no modification permitted": "0"},
    ),
    (
        "0BSD and Bison-exception-2.2",
        "LicenseRef-0",
        {"0BSD and Bison-exception-2.2": "0"},
    ),
    (
        "BSD and LGPLv2 and GPLv2",
        "LicenseRef-BSD AND LicenseRef-LGPLv2 AND LicenseRef-GPLv2",
        {
            "BSD": "BSD",
            "LGPLv2": "LGPLv2",
            "GPLv2": "GPLv2",
        },
    ),
]


@pytest.mark.parametrize("license_raw, license_valid, license_refs", license_expressions)
def test_validate_licenses(license_raw, license_valid, license_refs):
    manifest = ComponentManifestFile(ComponentFactory())
    result = str(manifest.validate_licenses(license_raw))
    assert license_refs == manifest.extracted_licenses
    assert result == license_valid


def test_component_manifest_properties():
    """Test that all Components have a .manifest property
    And that it generates valid JSON."""
    component, _, provided, dev_provided = setup_products_and_components_provides()

    component_manifest_file = ComponentManifestFile(component)
    manifest = component_manifest_file.render_content()

    document_namespace_prefix = "https://access.redhat.com/security/data/sbom/spdx/"
    assert (
        manifest["documentNamespace"]
        == f"{document_namespace_prefix}{component.name}-{component.version}"
    )

    num_provided = len(component.get_provides_pks())

    assert num_provided == 2

    # Last component is the one we're manifesting
    component_data = manifest["packages"][-1]

    # UUID, CPE, and PURL for the component we're manifesting should be included in manifest
    assert component_data["SPDXID"] == f"SPDXRef-{component.uuid}"
    assert component_data["name"] == component.name
    assert component_data["packageFileName"] == f"{component.nevra}.rpm"
    assert component_data["externalRefs"][0]["referenceLocator"] == component.purl
    assert component_data["externalRefs"][-1]["referenceLocator"] == component.cpes.get()

    document_describes_product = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "DESCRIBES",
        "spdxElementId": "SPDXRef-DOCUMENT",
    }

    provided_contained_by_component = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "CONTAINED_BY",
        "spdxElementId": f"SPDXRef-{provided.uuid}",
    }

    dev_provided_dependency_of_component = {
        "relatedSpdxElement": f"SPDXRef-{component.uuid}",
        "relationshipType": "DEV_DEPENDENCY_OF",
        "spdxElementId": f"SPDXRef-{dev_provided.uuid}",
    }

    assert len(manifest["relationships"]) == num_provided + 1
    # Relationships in manifest use a constant ordering based on object UUID
    # Actual UUIDs created in the tests will vary, so make sure we assert the right thing
    if dev_provided.uuid < provided.uuid:
        dev_provided_index = 0
        provided_index = 1
    else:
        provided_index = 0
        dev_provided_index = 1

    assert manifest["relationships"][provided_index] == provided_contained_by_component
    assert manifest["relationships"][dev_provided_index] == dev_provided_dependency_of_component
    assert manifest["relationships"][-1] == document_describes_product

    extracted_licensing_info = manifest["hasExtractedLicensingInfos"]
    assert len(extracted_licensing_info) == 1
    invalid_license_declared = (
        "BSD-3-Clause or (GPLv3+ with exceptions and LGPLv3+) and Public Domain"
    )
    expected_extracted_licensing_info = [
        {
            "comment": component_manifest_file.LICENSE_EXTRACTED_COMMENT,
            "extractedText": component_manifest_file.LICENSE_DECLARED_EXTRACTED_TEMPLATE.substitute(
                license_name=invalid_license_declared
            ),
            "licenseId": "LicenseRef-0",
            "name": "BSD-3-Clause or (GPLv3+ with exceptions and LGPLv3+) and Public Domain",
        },
    ]
    assert expected_extracted_licensing_info == extracted_licensing_info


def setup_products_and_components_upstreams():
    stream, variant = setup_product()
    meta_attr = {"released_errata_tags": ["RHBA-2023:1234"]}

    build = SoftwareBuildFactory(
        build_id=1,
        meta_attr=meta_attr,
    )

    other_build = SoftwareBuildFactory(
        build_id=2,
        meta_attr=meta_attr,
    )

    upstream = UpstreamComponentFactory()
    component = ContainerImageComponentFactory(software_build=build)
    other_component = ContainerImageComponentFactory(software_build=other_build)
    cnode = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=component,
    )
    other_cnode = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=other_component,
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=cnode,
        obj=upstream,
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=other_cnode,
        obj=upstream,
    )
    # Link the components to each other
    component.save_component_taxonomy()
    other_component.save_component_taxonomy()

    # The product_ref here is a variant name but below we use it's parent stream
    # to generate the manifest
    ProductComponentRelationFactory(
        software_build=build,
        build_id=build.build_id,
        build_type=build.build_type,
        product_ref=variant.name,
        type=ProductComponentRelation.Type.ERRATA,
    )
    ProductComponentRelationFactory(
        software_build=other_build,
        build_id=other_build.build_id,
        build_type=other_build.build_type,
        product_ref=variant.name,
        type=ProductComponentRelation.Type.ERRATA,
    )
    # Link the components to the ProductModel instances
    build.save_product_taxonomy()
    other_build.save_product_taxonomy()

    return stream, component, other_component, upstream


def setup_products_and_components_provides(released=True, internal_component=False):
    stream, variant = setup_product()
    build = SoftwareBuildFactory(build_id=1)
    if released:
        ProductComponentRelationFactory(
            software_build=build,
            build_id=build.build_id,
            build_type=build.build_type,
            product_ref=variant.name,
            type=ProductComponentRelation.Type.ERRATA,
        )

    if internal_component:
        provided = UpstreamComponentFactory(name="blah.redhat.com/", type=Component.Type.GOLANG)
        dev_provided = UpstreamComponentFactory(name="github.com/blah", type=Component.Type.NPM)
    else:
        provided = BinaryRpmComponentFactory()
        dev_provided = UpstreamComponentFactory(type=Component.Type.NPM)
    component = SrpmComponentFactory(
        software_build=build,
    )
    cnode = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=component,
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.PROVIDES,
        parent=cnode,
        obj=provided,
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.PROVIDES_DEV,
        parent=cnode,
        obj=dev_provided,
    )
    # Link the components to each other
    component.save_component_taxonomy()

    # The product_ref here is a variant name but below we use it's parent stream
    # to generate the manifest
    ProductComponentRelationFactory(
        software_build=build,
        build_id=build.build_id,
        build_type=build.build_type,
        product_ref=stream.name,
        type=ProductComponentRelation.Type.BREW_TAG,
    )
    # Link the components to the ProductModel instances
    build.save_product_taxonomy()
    return component, stream, provided, dev_provided


def setup_products_and_rpm_in_containers():
    stream, variant = setup_product()
    rpm_in_container = BinaryRpmComponentFactory()
    containers = []
    for name in ["", "", "some-container-source"]:
        build, container = _build_rpm_in_containers(rpm_in_container, name=name, stream=stream)

        # The product_ref here is a variant name but below we use it's parent stream
        # to generate the manifest
        ProductComponentRelationFactory(
            software_build=build,
            build_id=build.build_id,
            build_type=build.build_type,
            product_ref=variant.name,
            type=ProductComponentRelation.Type.ERRATA,
        )
        # Link the components to the ProductModel instances
        build.save_product_taxonomy()
        containers.append(container)
    return containers, stream, rpm_in_container


def _build_rpm_in_containers(rpm_in_container, name="", stream=None):
    build = SoftwareBuildFactory(
        meta_attr={"released_errata_tags": ["RHBA-2023:1234"]},
    )
    if not name:
        container = ContainerImageComponentFactory(
            software_build=build,
        )
    else:
        container = ContainerImageComponentFactory(
            name=name,
            software_build=build,
        )
    if stream:
        container.productstreams.add(stream)
        container.save()
    cnode = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=container,
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.PROVIDES,
        parent=cnode,
        obj=rpm_in_container,
    )
    # Link the components to each other
    container.save_component_taxonomy()
    return build, container


def test_provided_relationships():
    """Test that nodes / components in a manifest are linked to each other
    using the correct relationship type"""
    # Arch-specific containers are a VARIANT_OF the parent noarch container
    # Regardless of node type
    purl = f"pkg:{Component.Type.CONTAINER_IMAGE.lower()}/name@version"
    node_type = ComponentNode.ComponentNodeType.PROVIDES
    assert provided_relationship(purl, node_type) == "VARIANT_OF"
    node_type = ComponentNode.ComponentNodeType.PROVIDES_DEV
    assert provided_relationship(purl, node_type) == "VARIANT_OF"

    # All other component types are either a DEV_DEPENDENCY_OF their parent
    purl = f"pkg:{Component.Type.RPM.lower()}/name@version"
    assert provided_relationship(purl, node_type) == "DEV_DEPENDENCY_OF"
    purl = f"pkg:{Component.Type.GOLANG.lower()}/name@version"
    assert provided_relationship(purl, node_type) == "DEV_DEPENDENCY_OF"

    # Or CONTAINED_BY their parent, depending on node type
    node_type = ComponentNode.ComponentNodeType.PROVIDES
    assert provided_relationship(purl, node_type) == "CONTAINED_BY"
    purl = f"pkg:{Component.Type.RPM.lower()}/name@version"
    assert provided_relationship(purl, node_type) == "CONTAINED_BY"


def test_same_contents(stored_proc):
    existing_file = "tests/data/manifest/sbom.json"
    cpe = "cpe:/a:redhat:external_name:1.0::el8"
    external_name = "external-name-1.0"
    stream = ProductStreamFactory(name=external_name, version="1.0")
    TaskResult.objects.create(
        task_name="corgi.tasks.manifest.cpu_update_ps_manifest",
        task_args=f"\"('{external_name}', 'EXTERNAL-NAME-1.0')\"",
        status="SUCCESS",
        result='[true, "2024-01-31T00:22:29Z", "SPDXRef-303ea2fd-1a48-4590-90b4-4fd901272ca3"]',
    )
    stream_node = ProductStreamNodeFactory(obj=stream)
    variant = ProductVariantFactory(cpe=cpe)
    ProductVariantNodeFactory(obj=variant, parent=stream_node)
    stream.refresh_from_db()
    is_same_contents, new_content = same_contents(existing_file, stream)
    assert is_same_contents
    assert not new_content


def test_different_contents(stored_proc):
    missing_file = "tests/data/manifest/missing.json"
    external_name = "external-name-1.0"
    stream = ProductStreamFactory(name=external_name, version="1.0")
    is_same_contents, new_content = same_contents(missing_file, stream)
    assert not is_same_contents
    assert not new_content

    # test missing result
    existing_file = "tests/data/manifest/sbom.json"
    last_successful_created_at_date = "2024-01-31T00:22:29Z"
    last_successful_document_id = "SPDXRef-303ea2fd-1a48-4590-90b4-4fd901272ca3"

    is_same_contents, new_content = same_contents(existing_file, stream)
    assert not is_same_contents
    assert not new_content

    # test different content
    existing_file = "tests/data/manifest/sbom.json"
    TaskResult.objects.create(
        task_name="corgi.tasks.manifest.cpu_update_ps_manifest",
        task_args=f"\"('{external_name}', 'EXTERNAL-NAME-1.0')\"",
        status="SUCCESS",
        result=f'[true, "{last_successful_created_at_date}", "{last_successful_document_id}"]',
    )
    # This allows the ofuri value to work during manifest creation
    ProductStreamNodeFactory(obj=stream)
    stream.refresh_from_db()
    # Don't create and link a variant so there is not cpe value in the content (a mismatch)

    is_same_contents, new_content = same_contents(existing_file, stream)
    assert not is_same_contents
    assert new_content["creationInfo"]["created"] != last_successful_created_at_date
    # document_uuid is populated
    assert new_content["packages"][0]["SPDXID"] != last_successful_document_id
    assert new_content["relationships"][0]["relatedSpdxElement"] != last_successful_document_id

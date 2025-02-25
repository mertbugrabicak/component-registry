from unittest.mock import patch

import pytest

from corgi.collectors.brew import Brew
from corgi.collectors.models import CollectorRhelModule, CollectorRPM, CollectorSRPM
from corgi.collectors.rhel_compose import RhelCompose
from corgi.core.models import Component, ProductComponentRelation, ProductStream
from corgi.tasks.brew import slow_fetch_modular_build
from corgi.tasks.rhel_compose import get_builds, save_compose
from tests.factories import ProductVersionFactory
from tests.test_products import TEST_DOWNLOAD_URL

pytestmark = pytest.mark.unit

module_build_id = "0"
module_nvr = "389-ds-1.4-8060020220204145416.ce3e8c9c"
srpm_build_id = 1
rpm_nvr = "389-ds-base-1.4.3.28-6.module+el8.6.0+14129+983ceada.x86_64"


@pytest.mark.django_db
@patch("corgi.collectors.brew.Brew.brew_srpm_lookup")
@patch("corgi.collectors.brew.Brew.brew_rpm_lookup")
def test_fetch_module_data(mock_brew_rpm_lookup, mock_brew_srpm_lookup, requests_mock):
    compose_url = f"{TEST_DOWNLOAD_URL}/rhel-8/rel-eng/RHEL-8/latest-RHEL-8.6.0/compose/metadata/"
    with open("tests/data/compose/RHEL-8.6.0-20220420.3/modules.json") as compose:
        requests_mock.get(f"{compose_url}modules.json", text=compose.read())

    rpm_result = MockBrewResult()
    rpm_result.result = {"build_id": 1875692}
    mock_brew_rpm_lookup.return_value = [
        ("389-ds-base-1.4.3.28-6.module+el8.6.0+14129+983ceada.x86_64", rpm_result)
    ]

    srpm_result = MockBrewResult()
    srpm_result.result = 1875729
    mock_brew_srpm_lookup.return_value = [("389-ds-1.4-8060020220204145416.ce3e8c9c", srpm_result)]

    assert CollectorRhelModule.objects.all().count() == 0
    assert CollectorSRPM.objects.all().count() == 0
    assert CollectorRPM.objects.all().count() == 0

    module_srpms = RhelCompose._fetch_module_data(compose_url, ["BaseOS", "SAPHANA"])
    assert 1875729 in list(module_srpms)
    rhel_module = CollectorRhelModule.objects.get(nvr="389-ds-1.4-8060020220204145416.ce3e8c9c")
    assert rhel_module.build_id == 1875729
    srpm = CollectorSRPM.objects.get(build_id=1875692)
    assert srpm

    rpm = CollectorRPM.objects.get(
        nvra="389-ds-base-1.4.3.28-6.module+el8.6.0+14129+983ceada.x86_64"
    )
    assert rpm
    assert rpm.rhel_module.first() == rhel_module
    assert rpm.srpm == srpm


@pytest.mark.django_db
@patch("corgi.tasks.brew.slow_fetch_brew_build.delay")
@patch("corgi.collectors.brew.Brew.fetch_rhel_module", return_value={})
def test_get_builds(mock_fetch_rhel_module, mock_slow_fetch_brew_build):
    get_builds()
    mock_fetch_rhel_module.assert_not_called()
    mock_slow_fetch_brew_build.assert_not_called()
    ProductComponentRelation.objects.create(
        type=ProductComponentRelation.Type.COMPOSE, build_id=module_build_id
    )
    with patch(
        "corgi.tasks.brew.slow_fetch_modular_build.apply_async",
        return_value=slow_fetch_modular_build(module_build_id),
    ) as mock_fetch_compose:
        get_builds()
        mock_fetch_compose.assert_called_once_with(
            args=("0",), kwargs={"force_process": False}, priority=0
        )
    mock_fetch_rhel_module.assert_called_once_with("0")
    mock_slow_fetch_brew_build.assert_called_once_with("0", force_process=False)


@pytest.mark.django_db
@patch("corgi.tasks.brew.slow_fetch_brew_build.apply_async")
def test_fetch_compose_build(mock_fetch_brew):
    modular_rpm = _set_up_rhel_compose()
    slow_fetch_modular_build(module_build_id)

    module_obj = Component.objects.get(type=Component.Type.RPMMOD)
    assert module_obj.nvr == modular_rpm.rhel_module.get().nvr
    assert module_obj.cnodes.count() == 1

    modular_rpm_obj = Component.objects.get(type=Component.Type.RPM)
    assert not modular_rpm_obj.software_build
    assert modular_rpm.nvra == f"{modular_rpm_obj.nvr}.{modular_rpm_obj.arch}"
    assert mock_fetch_brew.called_once_with(srpm_build_id)


@pytest.mark.django_db
@patch("corgi.tasks.brew.slow_fetch_brew_build.apply_async")
def test_updated_module_node_purl_generation(mock_fetch_brew):
    """Test that purls on RPM modules, and on ComponentNodes linked to them,
    are generated and saved correctly on both whenever the component changes"""
    modular_rpm = _set_up_rhel_compose()
    slow_fetch_modular_build(module_build_id)
    module_obj = Component.objects.get(type=Component.Type.RPMMOD)
    module_node = module_obj.cnodes.get()

    # name-version-release.commit_id in an NVR
    # becomes name@version:release:commit_id in a purl
    modular_nvr = modular_rpm.rhel_module.get().nvr
    modular_purl = modular_nvr.replace("-", "@", 2).replace("-", ":", 1).replace(".", ":", 2)
    # Put back the extra - in the name and the . in the version
    # No simple way to avoid replacing these with @ and : above
    modular_purl = modular_purl.replace("@", "-", 1).replace(":", ".", 1)
    modular_purl = (
        f"pkg:{Component.Type.RPMMOD.lower()}/{Component.Namespace.REDHAT.lower()}/{modular_purl}"
    )

    # Purls should match on both the module and all its nodes
    assert module_obj.purl == modular_purl
    assert module_node.purl == modular_purl
    assert mock_fetch_brew.called_once_with(srpm_build_id)
    mock_fetch_brew.reset_mock()

    # Pretend this module was originally missing "/redhat/" in its purl
    # AKA it was created with an empty "" string namespace by mistake
    module_obj.namespace = ""
    module_obj.save()
    module_node.save()
    # Purls should match on both the module and all its nodes
    assert module_obj.purl == modular_purl.replace("/redhat/", "/", 1)
    assert module_node.purl == modular_purl.replace("/redhat/", "/", 1)

    # When we force-reprocess a component, its purl may change
    # e.g. to set the namespace correctly if we missed this before
    slow_fetch_modular_build(module_build_id, force_process=True)
    module_obj.refresh_from_db()
    module_node.refresh_from_db()
    assert module_obj.namespace == Component.Namespace.REDHAT

    # The purl on all of its currently-existing nodes should be updated to match
    # so we shouldn't create new nodes with the new purl
    # and we shouldn't leave behind duplicate nodes with the old purl
    assert module_obj.purl == modular_purl
    assert module_obj.cnodes.count() == 1
    assert module_node.purl == modular_purl
    assert mock_fetch_brew.called_once_with(srpm_build_id)


@pytest.mark.django_db
def test_fetch_rhel_module():
    _set_up_rhel_compose()
    assert not Brew.fetch_rhel_module(2)
    rhel_module_component = Brew.fetch_rhel_module(module_build_id)
    assert rhel_module_component
    assert len(rhel_module_component["components"]) == 1
    assert len(rhel_module_component["nested_builds"]) == 1
    assert (
        rhel_module_component["components"][0]["meta"]["release"]
        == "6.module+el8.6.0+14129+983ceada"
    )
    assert rhel_module_component["components"][0]["meta"]["arch"] == "x86_64"

    assert Brew.fetch_rhel_module(module_nvr)


def _set_up_rhel_compose() -> CollectorRPM:
    rhel_module = CollectorRhelModule.objects.create(build_id=module_build_id, nvr=module_nvr)
    srpm = CollectorSRPM.objects.create(build_id=srpm_build_id)
    rpm = CollectorRPM.objects.create(nvra=rpm_nvr, srpm=srpm)
    rpm.rhel_module.set([rhel_module])
    return rpm


@patch("corgi.collectors.brew.Brew.brew_srpm_lookup")
def test_fetch_rpm_data(mock_brew_srpm_lookup, requests_mock):
    compose_url = f"{TEST_DOWNLOAD_URL}/rhel-8/rel-eng/RHEL-8/RHEL-8.4.0-RC-1.2/compose/metadata"
    with open("tests/data/compose/RHEL-8.4.0-RC-1.2/rpms.json") as module_data:
        requests_mock.get(f"{compose_url}rpms.json", text=module_data.read())
    result = MockBrewResult()
    result.result = "1533085"
    mock_brew_srpm_lookup.return_value = [
        ("389-ds-base-1.4.3.16-13.module+el8.4.0+10307+74bbfb4e", result)
    ]
    srpms = RhelCompose._fetch_rpm_data(compose_url, ["AppStream"])
    assert "1533085" in list(srpms)


class MockBrewResult(object):
    pass


def mock_fetch_rpm_data(compose_url, variants):
    yield "1533085"


@pytest.mark.django_db
@patch("corgi.tasks.brew.slow_fetch_modular_build.apply_async")
@patch("corgi.collectors.rhel_compose.RhelCompose._fetch_rpm_data", new=mock_fetch_rpm_data)
def test_save_compose(mock_fetch_modular_build, requests_mock):
    composes = {
        f"{TEST_DOWNLOAD_URL}/rhel-8/rel-eng/RHEL-8/RHEL-8.4.0-RC-1.2/compose": ["AppStream"]
    }
    compose_url = next(iter(composes))
    for path in ("composeinfo", "rpms", "osbs", "modules"):
        with open(f"tests/data/compose/RHEL-8.4.0-RC-1.2/{path}.json") as compose:
            requests_mock.get(f"{compose_url}/metadata/{path}.json", text=compose.read())
    product_version = ProductVersionFactory()
    product_stream = ProductStream.objects.create(
        name="rhel-8.4.0",
        composes=composes,
        products=product_version.products,
        productversions=product_version,
    )
    save_compose("rhel-8.4.0")
    relation = ProductComponentRelation.objects.get(product_ref=product_stream)
    assert relation.type == ProductComponentRelation.Type.COMPOSE
    assert relation.external_system_id == "RHEL-8.4.0-20210503.1"
    assert requests_mock.call_count == 3  # TODO: Request for one path isn't being made?
    mock_fetch_modular_build.assert_called_once_with(kwargs={"build_id": "1533085"}, priority=0)

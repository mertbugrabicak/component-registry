import copy
import os
import shutil
import subprocess
from pathlib import Path, PosixPath
from typing import Tuple
from unittest.mock import call, patch

import pytest
from django.conf import settings
from packageurl import PackageURL

from corgi.collectors.go_list import GoList
from corgi.collectors.syft import Syft
from corgi.core.constants import RED_HAT_MAVEN_REPOSITORY
from corgi.core.models import Component, ComponentNode
from corgi.tasks.sca import (
    _clone_source,
    _download_lookaside_sources,
    _get_distgit_sources,
    _scan_files,
    cpu_software_composition_analysis,
    save_component,
)
from tests.factories import (
    ComponentFactory,
    ContainerImageComponentFactory,
    SoftwareBuildFactory,
    SrpmComponentFactory,
)

GO_PACKAGE_VERSION = "1.0.0"

GO_STDLIB_VERSION = "1.18.0"

pytestmark = pytest.mark.unit


def test_parse_components():
    with open("tests/data/crio-syft.json", "r") as crio_test_data:
        results, _ = Syft.parse_components(crio_test_data.read())
    assert len(results) > 0
    names = [r["meta"]["name"] for r in results]
    # When vendor directories are included we get entries like this because of relative replace
    # directives in nested go.mod files
    assert "../" not in names
    assert "github.com/Microsoft/go-winio" in names
    assert results[0]["meta"]["source"] == ["syft-0.60.1"]


def test_parse_maven_components():
    with open("tests/data/hawkular-metrics-schema-installer-syft.json", "r") as maven_test_data:
        results, _ = Syft.parse_components(maven_test_data.read())
    assert len(results) > 0
    group_ids = [r["meta"].get("group_id") for r in results if r["type"] == Component.Type.MAVEN]
    assert None not in group_ids
    assert "com.github.jnr" in group_ids


def test_parse_golist_components():
    results = []
    with open("tests/data/go/runc-1.1.3-golist.json") as runc_go_list_data:
        for result in GoList.parse_components(runc_go_list_data):
            results.append(result)

    names = [r["meta"]["name"] for r in results]
    assert len(set(names)) == len(results)
    assert "crypto/tls" in names


go_sources = [
    ("tests/data/go/runc-1.1.3.tar.gz"),
    ("tests/data/go/podman-4.3.1-814b7b0.tar.gz"),
]


@pytest.mark.parametrize("go_source", go_sources)
def test_golist_scan_files(go_source):
    with Path("Dockerfile") as scan_file:
        assert not scan_file.is_dir()
        assert not GoList.scan_files([scan_file])
    with Path(go_source) as archive:
        assert GoList.scan_files([archive])


@pytest.mark.django_db
@patch("corgi.collectors.go_list.GoList.scan_files")
def test_add_go_stdlib_version(go_list_scan_files):
    go_list_scan_files.return_value = [
        {"type": Component.Type.GOLANG, "meta": {"name": "go-package"}}
    ]
    root_component = ContainerImageComponentFactory(
        meta_attr={"go_stdlib_version": GO_STDLIB_VERSION}
    )
    anchor_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=root_component,
    )
    _scan_files(anchor_node, [])
    # Verify we can look-up the detected go package with stdlib version added from root node
    Component.objects.get(type=Component.Type.GOLANG, name="go-package", version=GO_STDLIB_VERSION)


@pytest.mark.django_db
@patch("corgi.collectors.go_list.GoList.scan_files")
def test_anchor_node_without_go_stdlib_version(go_list_scan_files):
    go_list_scan_files.return_value = [
        {"type": Component.Type.GOLANG, "meta": {"name": "go-package"}, "analysis_meta": {}}
    ]
    root_component = ContainerImageComponentFactory()
    anchor_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=root_component,
    )
    _scan_files(anchor_node, [])
    go_package = Component.objects.get(type=Component.Type.GOLANG, name="go-package")
    # Verify we can lookup there was go no stdlib version added to the detected go packed
    assert go_package.version == ""


@pytest.mark.django_db
@patch("corgi.collectors.go_list.GoList.scan_files")
def test_go_package_with_version(go_list_scan_files):
    go_list_scan_files.return_value = [
        {
            "type": Component.Type.GOLANG,
            "meta": {"name": "go-package", "version": GO_PACKAGE_VERSION},
        }
    ]
    root_component = ContainerImageComponentFactory()
    anchor_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=root_component,
    )
    _scan_files(anchor_node, [])
    go_package = Component.objects.get(type=Component.Type.GOLANG, name="go-package")
    # Verify there was no modification to the go package version if it was set by the collector
    assert go_package.version == GO_PACKAGE_VERSION


@pytest.mark.django_db
@patch("corgi.collectors.go_list.GoList.scan_files")
def test_go_package_type(go_list_scan_files):
    go_list_scan_files.return_value = [
        {
            "type": Component.Type.GOLANG,
            "meta": {
                "name": "go-package",
                "version": GO_PACKAGE_VERSION,
                "go_component_type": "go-package",
            },
        }
    ]
    root_component = ContainerImageComponentFactory()
    anchor_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=root_component,
    )
    _scan_files(anchor_node, [])
    go_package = Component.objects.get(type=Component.Type.GOLANG, name="go-package")
    # Verify there was no modification to the go package version if it was set by the collector
    assert go_package.meta_attr["go_component_type"] == "go-package"


dist_git_hostname = os.environ["CORGI_LOOKASIDE_CACHE_URL"]
dist_git_hostname = dist_git_hostname.replace("https://", "", 1)
dist_git_hostname = dist_git_hostname.replace("/repo", "", 1)

# Should succeed
archive_source_test_data = (
    (
        f"git://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+https://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+http://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+http://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+http://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+http://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+ssh://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+ssh://{dist_git_hostname}"  # Comma not missing, joined with below
        "/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console.git",
        "2",
        "/tmp/2",
    ),
    (
        f"git+ssh://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console",
        "2",
        "/tmp/2",
    ),
    (
        f"git+ssh://{dist_git_hostname}"  # Comma not missing, joined with below
        "/git/containers/openshift-enterprise-console.git",
        "f95972ce68d2850ae20c10fbf87182a17fa24b19",
        "openshift-enterprise-console.git",
        "2",
        "/tmp/2",
    ),
)


@pytest.mark.parametrize(
    "source_url,commit,expected_name,build_id,expected_path", archive_source_test_data
)
@patch("subprocess.check_call")
def test_clone_source(mock_subprocess, source_url, commit, expected_name, build_id, expected_path):
    """Test that _clone_source() works correctly for various historical Brew build source URLs
    Note we only test the dist-git server here - other git services have different requirements"""
    # This prevents multiple runs of this test from having different results because
    # we mkdir the directory prior to the clone
    shutil.rmtree(expected_path, ignore_errors=True)

    # Protocol must be https:// or (rarely) ssh:// or (even more rarely) http://
    # git:// or git+XYZ:// is no longer allowed
    if source_url.startswith("git://"):
        clean_source_url = source_url.replace("git://", "https://", 1)
    else:
        clean_source_url = source_url.replace("git+", "", 1)

    # Web-based http:// or https:// URLs must have "/git/" at start of path
    if clean_source_url.startswith("http") and "/git/" not in source_url:
        clean_source_url = clean_source_url.replace("/containers", "/git/containers", 1)

    git_clone_call = call(["/usr/bin/git", "clone", clean_source_url, PosixPath(expected_path)])
    retry_clone_call = call(
        ["/usr/bin/git", "clone", clean_source_url.replace(".git", "", 1), PosixPath(expected_path)]
    )
    git_checkout_call = call(
        ["/usr/bin/git", "checkout", commit], cwd=PosixPath(expected_path), stderr=-3
    )

    if clean_source_url.startswith("http") and ".git" in source_url:
        # First call (git clone) fails, second (retry git clone) and third (git checkout) succeed
        git_clone_error = subprocess.CalledProcessError(128, "/usr/bin/git clone")
        mock_subprocess.side_effect = (git_clone_error, None, None)
        calls = (git_clone_call, retry_clone_call, git_checkout_call)
    else:
        # First call (git clone) succeeds, no need for a retry
        calls = (git_clone_call, git_checkout_call)

    target_path, package_type, package_name = _clone_source(f"{source_url}#{commit}", build_id)
    assert PosixPath(expected_path) == target_path
    assert expected_name == package_name
    mock_subprocess.assert_has_calls(calls)


archive_source_test_data_for_failures = (
    # Fails due to ValueError - only git://, git+https://, etc. or https:// protocol supported
    (
        "http://src.fedoraproject.org/rpms/nodejs",
        "3cbed2be4171502499d0d89bea1ead91690af7d2",
        "nodejs",
        "1",
        "/tmp/1",
        False,
    ),
    # Fails due to ValueError - path too short
    (
        "git://src.fedoraproject.org/tooshort",
        "3cbed2be4171502499d0d89bea1ead91690af7d2",
        "nodejs",
        "1",
        "/tmp/1",
        False,
    ),
    # Fails due to FileExistsError
    (
        "git+https://src.fedoraproject.org/rpms/nodejs",
        "3cbed2be4171502499d0d89bea1ead91690af7d2",
        "nodejs",
        "1",
        "/tmp/1",
        True,
    ),
    # Fails due to FileExistsError
    # AKA https:// URLs get past the "is this scheme supported?" check
    (
        "https://src.fedoraproject.org/rpms/nodejs",
        "3cbed2be4171502499d0d89bea1ead91690af7d2",
        "nodejs",
        "1",
        "/tmp/1",
        True,
    ),
)


@pytest.mark.parametrize(
    "source_url,commit,package_name,build_id,expected_path,path_exists",
    archive_source_test_data_for_failures,
)
@patch("subprocess.check_call")
def test_clone_source_for_failures(
    mock_subprocess, source_url, commit, package_name, build_id, expected_path, path_exists
):
    # This prevents multiple runs of this test from having different results because
    # we mkdir the directory prior to the clone
    if not path_exists:
        shutil.rmtree(expected_path, ignore_errors=True)
        with pytest.raises(ValueError):
            _clone_source(f"{source_url}#{commit}", build_id)
    else:
        expected_path_obj = Path(expected_path)
        if not expected_path_obj.exists():
            os.mkdir(f"/tmp/{build_id}")
        with pytest.raises(FileExistsError):
            _clone_source(f"{source_url}#{commit}", build_id)
    mock_subprocess.assert_not_called()


@patch("subprocess.check_call")
@patch("corgi.tasks.sca._download_lookaside_sources")
def test_get_distgit_sources(mock_check_call, mock_download_lookaside_sources):
    shutil.rmtree("/tmp/1")
    expected_path = "/tmp/1"
    _ = _get_distgit_sources(
        f"git://{os.getenv('CORGI_LOOKASIDE_CACHE_URL')}"  # Comma not missing, joined with below
        "/rpms/test#1e52fcdc84be253b5094b942c2fec23d7636d644",
        1,
    )
    mock_check_call.assert_called_with(PosixPath(expected_path), 1, "rpms", "test")


download_lookaside_test_data = [
    (
        # $BREW_URL/buildinfo?buildID=1210605
        # spec file removed
        "tests/data/rpms/containernetworking-plugins",
        "containernetworking-plugins",
        "rpms",
        "v0.8.6.tar.gz",
        "md5/85eddf3d872418c1c9d990ab8562cc20/",
    ),
    (
        # Nothing gets downloaded because the sources file in the distgit source is empty
        "tests/data/containers/openshift-enterprise-hyperkube",
        "openshift-enterprise-hyperkube",
        "containers",
        None,
        None,
    ),
    (
        # buildID=2096033
        # Dummy distgit archive with all but 'sources' file removed
        "tests/data/containers/metrics-schema-installer",
        "metrics-schema-installer",
        "containers",
        "hawkular-metrics-schema-installer-0.31.0.Final-redhat-1.jar",
        "md5/587372e4c72d1eddfab8e848457f574e/",
    ),
]


@pytest.mark.parametrize(
    "test_sources,package_name,package_type,expected_filename,expected_path",
    download_lookaside_test_data,
)
def test_download_lookaside_sources(
    test_sources, package_name, package_type, expected_filename, expected_path, requests_mock
):
    distgit_source_archive = Path(test_sources)
    expected_url = (
        f"{settings.LOOKASIDE_CACHE_BASE_URL}/{package_type}/{package_name}/"
        f"{expected_filename}/{expected_path}{expected_filename}"
    )
    print(f"mocking call to {expected_url}")
    requests_mock.get(expected_url, text="resp")

    downloaded_sources = _download_lookaside_sources(
        distgit_source_archive, 1, package_type, package_name
    )
    if expected_filename:
        expected_hash_prefix = expected_path.split("/")[1][:6]
        full_expected_filename = f"/tmp/lookaside/1/{expected_hash_prefix}-{expected_filename}/"
        assert downloaded_sources == [PosixPath(full_expected_filename)]
        shutil.rmtree("/tmp/lookaside/1")
    else:
        assert downloaded_sources == []


slow_software_composition_analysis_test_data = [
    # Dummy tar files are prefetch to
    # tests/data/rpms/cri-o/sources
    # tests/data/lookaside/rpms/cri-o/cri-o-41c0779.tar.gz (empty file)
    (
        2018747,
        False,
        "cri-o",
        "a5afa6-cri-o-41c0779.tar.gz",
        "rpms/cri-o/cri-o-41c0779.tar.gz/sha512/a5afa6ce06992d3205ae06e1d5a25109c3ef5596bfaaf456f1c"
        "25f48d4fdb18607f43591dd75cad122fc2d5ddbb00451ad88de9420fa84175d52b010ff2a16ff/"
        "cri-o-41c0779.tar.gz",
        "tests/data/crio-syft.json",
        "pkg:golang/github.com/Microsoft/go-winio@v0.5.1",
    ),
    # Dummy tar file created in
    # tests/data/containers/<package-name> (without -container suffix)
    # These repos have no lookaside cache, so they set an empty lookaside_tarfile
    (
        1890406,
        True,
        "",
        "",
        "",
        "tests/data/grafana-syft.json",
        "pkg:npm/acorn-globals@4.3.4",
    ),
    (
        1888203,
        True,
        "",
        "",
        "",
        "tests/data/cnf-tests-syft.json",
        "pkg:pypi/requests@2.26.0",
    ),
    (
        2096033,
        True,
        "metrics-schema-installer",
        "587372-hawkular-metrics-schema-installer-0.31.0.Final-redhat-1.jar",
        "rpms/metrics-schema-installer/hawkular-metrics-schema-installer-0.31.0.Final-redhat-1.jar/"
        "md5/587372e4c72d1eddfab8e848457f574e/"
        "hawkular-metrics-schema-installer-0.31.0.Final-redhat-1.jar",
        "tests/data/hawkular-metrics-schema-installer-syft.json",
        f"pkg:maven/com.github.jnr/jffi@1.2.10.redhat-1?repository_url={RED_HAT_MAVEN_REPOSITORY}",
    ),
]


def mock_clone(package_name: str, build_uuid: str) -> Tuple[Path, str, str]:
    target_path = Path(f"/tmp/{build_uuid}")
    if not target_path.exists():
        os.mkdir(f"/tmp/{build_uuid}")
    # Copy test files into scratch location
    test_sources = Path(f"tests/data/sca/{package_name}/sources")
    if test_sources.exists():
        shutil.copyfile(test_sources, target_path / "sources")
    # TODO split package_type out to testdata
    package_type = "rpms"
    return target_path, package_type, package_name


@pytest.mark.django_db
@pytest.mark.parametrize(
    "build_id,is_container,package_name,lookaside_file,download_path,syft_results,expected_purl",
    slow_software_composition_analysis_test_data,
)
# mock the syft call to avoid having to have actual source code for the test
@patch("subprocess.check_output")
@patch("corgi.tasks.sca._clone_source", side_effect=mock_clone)
def test_slow_software_composition_analysis(
    mock_clone_source,
    mock_syft,
    build_id,
    is_container,
    package_name,
    lookaside_file,
    download_path,
    syft_results,
    expected_purl,
    requests_mock,
):
    sb = SoftwareBuildFactory(build_id=build_id, source=package_name)
    root_component = (
        ContainerImageComponentFactory(name="root_component", software_build=sb)
        if is_container
        else SrpmComponentFactory(name="root_component", software_build=sb)
    )
    ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=root_component,
    )
    assert not Component.objects.filter(purl=expected_purl).exists()

    expected_url = f"{settings.LOOKASIDE_CACHE_BASE_URL}/{download_path}"

    requests_mock.get(expected_url, text="resp")

    with open(syft_results, "r") as mock_scan_results:
        mock_syft.return_value = mock_scan_results.read()

    with patch("corgi.tasks.sca.app") as mock_app:
        cpu_software_composition_analysis(str(sb.pk))
        mock_app.send_task.assert_called_once_with(
            "corgi.tasks.common.slow_save_taxonomy", args=(str(sb.build_id), sb.build_type)
        )

    mock_clone_source.assert_called_once_with(package_name, str(sb.pk))
    expected_syft_call_arg_list = [
        call(
            [
                "/usr/bin/syft",
                "packages",
                "-q",
                "-o=syft-json",
                "--exclude=**/vendor/**",
                "--exclude=**/test/**",
                f"dir:/tmp/{sb.pk}",
            ],
            text=True,
        ),
    ]
    if lookaside_file:
        expected_syft_call_arg_list.append(
            call(
                [
                    "/usr/bin/syft",
                    "packages",
                    "-q",
                    "-o=syft-json",
                    "--exclude=**/vendor/**",
                    "--exclude=**/test/**",
                    f"file:/tmp/lookaside/{sb.pk}/{lookaside_file}",
                ],
                text=True,
            )
        )
    assert mock_syft.call_args_list == expected_syft_call_arg_list
    expected_component = Component.objects.get(purl=expected_purl)
    if is_container:
        Component.objects.get(type=Component.Type.CONTAINER_IMAGE, arch="noarch", software_build=sb)
    else:
        Component.objects.srpms().get(software_build=sb)
    assert expected_component.meta_attr["source"] == ["syft-0.60.1"]


@pytest.mark.django_db
def test_save_component_skips_duplicates():
    """Test that component names which only differ by dash / underscore,
    or different casing, do not create duplicate purls"""
    # Only type=PYPI doesn't distinguish between dash and underscore
    # Only type=PYPI and type=GITHUB don't distinguish between uppercase and lowercase
    old_component = ComponentFactory(
        type=Component.Type.PYPI,
        namespace=Component.Namespace.UPSTREAM,
        name="requests_ntlm",
        release="",
        arch="noarch",
    )
    image_component = ContainerImageComponentFactory(name="image_component")
    root_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=image_component,
    )

    new_component = {
        "type": old_component.type,
        "name": old_component.name.replace("_", "-").upper(),
        "version": old_component.version,
    }

    new_purl = PackageURL(**new_component).to_string()
    assert new_purl == old_component.purl

    new_component["meta"] = {"name": new_component["name"], "version": new_component["version"]}
    new_component_with_purl = copy.deepcopy(new_component)
    new_component_with_purl["meta"]["purl"] = new_purl

    # Find the existing component, don't create a duplicate
    # Create a new node and link it to the root
    assert old_component.cnodes.count() == 0
    assert save_component(new_component, root_node) is True
    assert not Component.objects.filter(name=new_component["name"]).exists()
    assert old_component.cnodes.count() == 1

    # Find the existing component, don't create a duplicate
    # Find the existing node, don't create a duplicate
    assert save_component(new_component_with_purl, root_node) is False
    assert not Component.objects.filter(name=new_component_with_purl["name"]).exists()
    assert old_component.cnodes.count() == 1


@pytest.mark.django_db
def test_save_component_handles_rpms():
    """Test that RPM components discovered by Syft, which may not
    have an epoch, arch, or release in their dict of component metadata,
    still get created with the correct values"""
    old_component = SrpmComponentFactory()
    root_node = ComponentNode.objects.create(
        type=ComponentNode.ComponentNodeType.SOURCE,
        parent=None,
        obj=old_component,
    )

    new_arch = "x86_64"
    new_component = {
        "type": old_component.type,
        "name": old_component.name,
        # Syft always reports a combined version-release as the discovered version
        # No separate release key here because Syft doesn't understand RPM versioning
        "version": f"{old_component.version}-{old_component.release}",
    }
    new_component["meta"] = {
        "name": new_component["name"],
        "version": new_component["version"],
        # RPM purls created by Syft are very different from RPM purls created by Corgi
        # We pull out and use only the epoch, arch, and release values
        # The whole Syft purl is also saved to the meta_attr
        # so other (unknown) qualifiers can be used in the future if needed
        "purl": f"pkg:rpm/redhatrhel/{old_component.name}@{old_component.version}"
        f"-{old_component.release}?arch={new_arch}"
        f"&upstream={old_component.nevra}.rpm&distro=rhel-X.y",
    }
    if old_component.epoch:
        new_component["meta"]["purl"] += f"&epoch={old_component.epoch}"

    dupe_component = copy.deepcopy(new_component)

    # Find the root component, create a child of it
    assert save_component(new_component, root_node) is True
    # Our code should split Syft's combined version-release into separate values,
    # then parse the qualifiers to determine epoch and arch
    # So the lookup below should always return exactly one result for the new RPM
    new_rpm = Component.objects.get(
        type=new_component["type"],
        namespace=old_component.namespace,
        name=new_component["name"],
        epoch=old_component.epoch,
        version=old_component.version,
        release=old_component.release,
        arch=new_arch,
    )
    assert new_rpm.cnodes.count() == 1
    # The whole Syft purl should be saved in the meta_attr
    assert new_rpm.meta_attr == {"purl": new_component["meta"]["purl"]}

    # Find the existing component, don't create a duplicate
    # Find the existing node, don't create a duplicate
    assert save_component(dupe_component, root_node) is False
    new_rpm.refresh_from_db()
    assert new_rpm.cnodes.count() == 1

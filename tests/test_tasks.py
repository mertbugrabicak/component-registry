from unittest.mock import call, patch

import pytest

from corgi.collectors.brew import ADVISORY_REGEX, Brew
from corgi.core.models import SoftwareBuild
from corgi.tasks.brew import slow_refresh_brew_build_tags, slow_update_brew_tags
from corgi.tasks.errata_tool import slow_handle_shipped_errata

from .factories import SoftwareBuildFactory

pytestmark = [
    pytest.mark.unit,
    pytest.mark.django_db(databases=("default",)),
]

# Raw tags with some data quality issues - we assert these are cleaned up
MISC_TAGS = ["stream-pending", "stream-candidate", "stream-pending", "RHXA-2023:1234-invalid"]
ERRATA_TAGS = [
    "RHBA-2023:1234567-pending",
    "RHEA-2023:12345-dropped",
    "RHBA-2023:1234567-alsopending",
]
RELEASED_TAGS = [
    "RHSA-2023:12345-pending",
    "RHEA-2023:1234-suffixignored",
    "RHEA-2023:1234-suffixignored",
]

EXISTING_TAGS = MISC_TAGS + ERRATA_TAGS + RELEASED_TAGS
CLEAN_TAGS = sorted(set(EXISTING_TAGS))
CLEAN_ERRATA_TAGS = sorted(set(tag.rsplit("-", maxsplit=1)[0] for tag in EXISTING_TAGS[4:]))
CLEAN_RELEASED_TAGS = sorted(set(tag.rsplit("-", maxsplit=1)[0] for tag in EXISTING_TAGS[8:]))

# TODO: Needs much more
# tag_added, is_errata, is_released
test_tag_data = (
    ("RHSA-2023:4321-released", True, True),
    ("RHSA-2023:43210-dropped", True, False),
    ("not_an_errata", False, False),
)


@pytest.mark.parametrize("tag_added,is_errata,is_released", test_tag_data)
def test_slow_update_brew_tags_added(tag_added, is_errata, is_released):
    """Test that builds have their tags and relations updated"""
    build = SoftwareBuildFactory(
        build_type=SoftwareBuild.Type.BREW, meta_attr={"tags": EXISTING_TAGS}
    )

    with patch("corgi.tasks.brew.slow_load_errata.delay") as mock_load_errata:
        slow_update_brew_tags(build.build_id, tag_added=tag_added)
    # Get updated build / tag data from DB after task saves it
    build = SoftwareBuild.objects.get(build_id=build.build_id, build_type=SoftwareBuild.Type.BREW)

    # Below are only modified if tag meets certain conditions
    clean_errata_tags = CLEAN_ERRATA_TAGS
    clean_released_tags = CLEAN_RELEASED_TAGS

    if is_errata:
        tag_without_suffix = tag_added.rsplit("-", maxsplit=1)[0]
        assert ADVISORY_REGEX.match(tag_added).group() == tag_without_suffix
        mock_load_errata.assert_called_once_with(tag_without_suffix)
        clean_errata_tags = sorted(set(clean_errata_tags + [tag_without_suffix]))

        if is_released:
            tag_id = tag_without_suffix.rsplit(":", maxsplit=1)[-1]
            assert len(tag_id) == 4 and tag_id.isdigit()
            clean_released_tags = sorted(set(clean_released_tags + [tag_without_suffix]))
    else:
        mock_load_errata.assert_not_called()

    # All tags end up in tags field
    # Only tags matching ADVISORY_REGEX end up in errata_tags field (with -suffix stripped)
    # Only released advisories (4-digit IDs) end up in released_errata_tags field
    # All fields are automatically sorted and deduped
    assert build.meta_attr["tags"] == sorted(set(CLEAN_TAGS + [tag_added]))
    assert build.meta_attr["errata_tags"] == clean_errata_tags
    assert build.meta_attr["released_errata_tags"] == clean_released_tags


def test_slow_update_brew_tags_removed():
    """Test that builds have their tags updated, but not relations"""
    tag_removed = RELEASED_TAGS[-1]
    tag_removed_without_suffix = tag_removed.rsplit("-", maxsplit=1)[0]
    build = SoftwareBuildFactory(build_type=SoftwareBuild.Type.BREW, meta_attr={"tags": CLEAN_TAGS})

    with patch("corgi.tasks.brew.slow_load_errata.delay") as mock_load_errata:
        slow_update_brew_tags(build.build_id, tag_removed=tag_removed)
    mock_load_errata.assert_not_called()

    # Get updated build / tag data from DB after task saves it
    build = SoftwareBuild.objects.get(build_id=build.build_id, build_type=SoftwareBuild.Type.BREW)

    clean_tags = sorted(set(tag for tag in CLEAN_TAGS if tag != tag_removed))
    clean_errata_tags = sorted(
        set(tag for tag in CLEAN_ERRATA_TAGS if tag != tag_removed_without_suffix)
    )
    clean_released_tags = sorted(
        set(tag for tag in CLEAN_RELEASED_TAGS if tag != tag_removed_without_suffix)
    )

    # All tags end up in tags field
    # Only tags matching ADVISORY_REGEX end up in errata_tags field (with -suffix stripped)
    # Only released advisories (4-digit IDs) end up in released_errata_tags field
    # All fields are automatically sorted and deduped
    assert build.meta_attr["tags"] == clean_tags
    assert build.meta_attr["errata_tags"] == clean_errata_tags
    assert build.meta_attr["released_errata_tags"] == clean_released_tags


def test_slow_update_brew_tags_errors():
    """Test that slow_update_brew_tags handles missing builds and missing tags"""
    warning = slow_update_brew_tags("123", tag_added="123")
    assert warning == "Brew build with matching ID not ingested (yet?): 123"

    # meta_attr field for all builds always has tags key set to a list (on ingestion)
    # no need to test missing tags key or values other than lists
    build = SoftwareBuildFactory(build_type=SoftwareBuild.Type.BREW, meta_attr={"tags": []})
    with pytest.raises(ValueError):
        # Must supply either tag_added or tag_removed kwarg
        slow_update_brew_tags(build.build_id)

    with pytest.raises(ValueError):
        # Raise an error if tag isn't found
        # This shouldn't happen unless we failed to add the tag in the first place
        # Probably worth reingesting at that point - explicit error reminds us to do so
        slow_update_brew_tags(build.build_id, tag_removed="does_not_exist")


@patch("corgi.tasks.errata_tool.app")
@patch("corgi.tasks.errata_tool.slow_load_errata")
@patch("corgi.tasks.errata_tool.ErrataTool")
def test_slow_handle_shipped_errata(mock_et_constructor, mock_load_errata, mock_app):
    """Test that builds have tags updated correctly when an erratum ships them"""
    erratum_id = 12345
    missing_build_id = 210
    existing_build = SoftwareBuildFactory()
    mock_et_collector = mock_et_constructor.return_value
    mock_et_collector.get.return_value = {
        "erratum_name": {
            "builds": [
                {"build_nevr": {"id": missing_build_id}},
                {"build_nevr": {"id": existing_build.build_id}},
            ]
        }
    }

    slow_handle_shipped_errata(erratum_id=erratum_id, erratum_status="SHIPPED_LIVE")

    mock_et_constructor.assert_called_once_with()
    mock_et_collector.get.assert_called_once_with(f"api/v1/erratum/{erratum_id}/builds_list")

    # Missing builds are fetched, existing builds have only their tags refreshed
    mock_fetch_brew_build_call = call(
        "corgi.tasks.brew.slow_fetch_brew_build",
        args=(missing_build_id, SoftwareBuild.Type.BREW),
    )
    mock_refresh_brew_build_tags_call = call(
        "corgi.tasks.brew.slow_refresh_brew_build_tags",
        args=(existing_build.build_id,),
    )
    mock_app.send_task.assert_has_calls(
        [mock_fetch_brew_build_call, mock_refresh_brew_build_tags_call]
    )

    # Taxonomy is always force-saved at the end
    mock_load_errata.delay.assert_called_once_with(str(erratum_id), force_process=True)


def test_slow_refresh_brew_build_tags():
    """Test that existing builds get their tags refreshed"""
    build = SoftwareBuildFactory(
        meta_attr={"tags": [], "errata_tags": [], "released_errata_tags": []}
    )
    with patch("corgi.tasks.brew.Brew", wraps=Brew) as mock_brew_constructor:
        # Wrap the mocked object, and call its methods directly
        # Unless we override / specify a return value here
        mock_brew_collector = mock_brew_constructor.return_value
        mock_brew_collector.koji_session.listTags.return_value = [
            {"name": tag} for tag in EXISTING_TAGS
        ]
        slow_refresh_brew_build_tags(build_id=build.build_id)

        mock_brew_constructor.assert_called_once_with(SoftwareBuild.Type.BREW)
        mock_brew_collector.koji_session.listTags.assert_called_once_with(build.build_id)

    build.refresh_from_db()
    assert build.meta_attr["tags"] == CLEAN_TAGS
    assert build.meta_attr["errata_tags"] == CLEAN_ERRATA_TAGS
    assert build.meta_attr["released_errata_tags"] == CLEAN_RELEASED_TAGS

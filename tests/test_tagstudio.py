import subprocess
import sys
from pathlib import Path

import pytest

from tagassist.tagstudio import TagStudioError, TagStudioLibrary

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_lib(tmp_path):
    root = tmp_path / "lib"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_sample_library.py"), str(root)],
        check=True,
        capture_output=True,
    )
    return root


def test_open_and_count(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        assert lib.entry_count() == 5
        entries = lib.entries()
        assert all(e.abs_path.exists() for e in entries)


def test_apply_tags_creates_categories_and_links(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        entry = lib.entries(limit=1)[0]
        added = lib.apply_tags(
            entry.id,
            {"People": ["Alice", "Bob"], "Location": ["Phoenix"], "Context": ["sunset hike"]},
        )
        assert set(added) == {"Alice", "Bob", "Phoenix", "sunset hike"}
        assert lib.tags_for_entry(entry.id) == ["Alice", "Bob", "Phoenix", "sunset hike"]
        # Category hierarchy was built
        assert lib.tags_under_category("People") == ["Alice", "Bob"]
        assert lib.tags_under_category("Location") == ["Phoenix"]


def test_tags_are_reused_not_duplicated(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        e1, e2 = lib.entries(limit=2)
        lib.apply_tags(e1.id, {"People": ["Alice"]})
        lib.apply_tags(e2.id, {"People": ["alice"]})  # same person, different case
        # Only one Alice tag exists overall
        alice_count = sum(1 for n in lib.all_tag_names() if n.lower() == "alice")
        assert alice_count == 1
        # ...but both entries are tagged
        assert "Alice" in lib.tags_for_entry(e1.id)
        assert lib.tags_for_entry(e2.id)  # linked to the same tag


def test_idempotent_tagging(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        entry = lib.entries(limit=1)[0]
        lib.apply_tags(entry.id, {"People": ["Alice"]})
        added_again = lib.apply_tags(entry.id, {"People": ["Alice"]})
        assert added_again == []  # nothing new added


def test_apply_entity_builds_nested_chain(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        entry = lib.entries(limit=1)[0]
        newly = lib.apply_entity(entry.id, "Stella", ["Pets", "Dog"])
        assert newly is True
        # Only the leaf is attached to the entry...
        assert lib.tags_for_entry(entry.id) == ["Stella"]
        # ...but the full hierarchy exists: Pets -> Dog -> Stella
        assert lib.tags_under_category("Pets") == ["Dog"]
        assert lib.tags_under_category("Dog") == ["Stella"]


def test_apply_entity_reuses_shared_parents(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        e1, e2 = lib.entries(limit=2)
        lib.apply_entity(e1.id, "Stella", ["Pets", "Dog"])
        lib.apply_entity(e2.id, "Rex", ["Pets", "Dog"])
        # Dog has two children now, Pets still has exactly one child (Dog)
        assert lib.tags_under_category("Dog") == ["Rex", "Stella"]
        assert lib.tags_under_category("Pets") == ["Dog"]
        # No duplicate Dog/Pets tags were created
        assert sum(1 for n in lib.all_tag_names() if n == "Dog") == 1
        assert sum(1 for n in lib.all_tag_names() if n == "Pets") == 1


def test_apply_entity_empty_chain(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        entry = lib.entries(limit=1)[0]
        assert lib.apply_entity(entry.id, "sunset", []) is True
        assert lib.tags_for_entry(entry.id) == ["sunset"]


def test_missing_library_raises(tmp_path):
    with pytest.raises(TagStudioError):
        TagStudioLibrary(tmp_path / "nope").connect()

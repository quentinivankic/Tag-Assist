import json
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


def test_learn_and_resolve_chain_from_db(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Phoenix", "Location > USA > Arizona")
        lib.learn("Moms House", "Phoenix")  # inherits Phoenix's ancestry
        assert lib.full_path("Moms House") == [
            "Location", "USA", "Arizona", "Phoenix", "Moms House",
        ]
        assert lib.resolve_chain("Phoenix") == ["Location", "USA", "Arizona"]
        assert lib.children("Phoenix") == ["Moms House"]
        assert lib.canonical_name("phoenix") == "Phoenix"  # case-insensitive


def test_leaf_descendants_and_tree(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Camelback Mountain", "Location > USA > Arizona")
        lib.learn("Moms House", "Location > USA > Arizona > Phoenix")
        lib.learn("South Mountain", "Phoenix")
        lib.learn("Tempe", "Arizona")  # a leaf city with no children
        # All bottom spots anywhere under Arizona, regardless of city depth.
        leaves = lib.leaf_descendants("Arizona")
        assert set(leaves) == {"Camelback Mountain", "Moms House", "South Mountain", "Tempe"}
        # Phoenix is NOT a leaf (it has children) so it isn't listed.
        assert "Phoenix" not in leaves
        # descendants() includes the intermediate Phoenix.
        assert "Phoenix" in lib.descendants("Arizona")
        # The full tree has Location as a root with USA beneath it.
        tree = {n["name"]: n for n in lib.tag_tree()}
        assert "Location" in tree
        usa = [c for c in tree["Location"]["children"] if c["name"] == "USA"]
        assert usa and any(c["name"] == "Arizona" for c in usa[0]["children"])


def test_bulk_apply_to_multiple_entries(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        e1, e2, e3 = lib.entries(limit=3)
        for e in (e1, e2, e3):
            lib.apply_entity(e.id, "Phoenix", ["Location", "USA", "Arizona"])
        for e in (e1, e2, e3):
            assert "Phoenix" in lib.tags_for_entry(e.id)


def test_resolve_chain_heals_multi_parent(sample_lib):
    # A stray direct USA->Phoenix link (from an earlier bug) must not shorten
    # the chain; resolve_chain picks the deepest parent (Arizona).
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Phoenix", "Location > USA > Arizona")
        usa, phx = lib.find_tag("USA"), lib.find_tag("Phoenix")
        lib._link_parent(usa, phx)  # inject the stray link
        lib.conn.commit()
        assert {n for _, n in lib._parents(phx)} == {"Arizona", "USA"}
        assert lib.resolve_chain("Phoenix") == ["Location", "USA", "Arizona"]


def test_collapse_descendants_from_db(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Phoenix", "Location > USA > Arizona")
        lib.learn("Moms House", "Phoenix")
        assert lib.collapse_descendants(["Phoenix", "Moms House"]) == ["Moms House"]
        assert lib.is_ancestor("Phoenix", "Moms House") is True
        assert lib.is_ancestor("Moms House", "Phoenix") is False


def test_suggest_match_from_db(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Camelback Mountain", "Location > USA > Arizona")
        assert lib.suggest_match("camel back mountain") == "Camelback Mountain"
        assert lib.suggest_match("camelbak mountain") == "Camelback Mountain"
        assert lib.suggest_match("Denmark") is None


def test_alias_recognized_from_db(sample_lib):
    with TagStudioLibrary(sample_lib) as lib:
        lib.learn("Camelback Mountain", "Location > USA > Arizona")
        assert lib.add_alias_by_name("Camelback Mountain", "camel back mountain") is True
        assert lib.canonical_name("camel back mountain") == "Camelback Mountain"


def test_import_legacy_entities(sample_lib):
    cache = Path(sample_lib) / ".tagassist_cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "entities.json").write_text(json.dumps({
        "phoenix": {"display": "Phoenix", "parent": "Arizona", "aliases": []},
        "arizona": {"display": "Arizona", "parent": "USA", "aliases": []},
        "usa": {"display": "USA", "parent": "Location", "aliases": []},
        "location": {"display": "Location", "parent": None, "aliases": []},
        "moms house": {"display": "Moms House", "parent": "Phoenix", "aliases": ["moms"]},
    }))
    with TagStudioLibrary(sample_lib) as lib:
        assert lib.import_legacy_entities() == 5
        assert lib.full_path("Moms House") == [
            "Location", "USA", "Arizona", "Phoenix", "Moms House",
        ]
        assert lib.canonical_name("moms") == "Moms House"  # alias imported
        # second run is a no-op (file already marked imported)
        assert lib.import_legacy_entities() == 0
    assert not (cache / "entities.json").exists()
    assert (cache / "entities.json.imported").exists()


def test_missing_library_raises(tmp_path):
    with pytest.raises(TagStudioError):
        TagStudioLibrary(tmp_path / "nope").connect()

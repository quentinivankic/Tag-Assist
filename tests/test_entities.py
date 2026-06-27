import json

from tagassist.entities import EntityStore, parse_chain


def test_parse_chain_separators():
    assert parse_chain("Pets > Dog") == ["Pets", "Dog"]
    assert parse_chain("Pets/Dog") == ["Pets", "Dog"]
    assert parse_chain("Dog") == ["Dog"]
    assert parse_chain("") == []
    assert parse_chain("  ") == []


def test_learn_and_lookup_case_insensitive(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Stella", "Pets > Dog")
    ent = store.lookup("stella")  # lowercase
    assert ent is not None
    assert ent.display == "Stella"
    assert store.resolve_chain("Stella") == ["Pets", "Dog"]
    assert store.full_path("Stella") == ["Pets", "Dog", "Stella"]


def test_alias_lookup(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Mom", ["People", "Family"], aliases=("mamma", "mother"))
    assert store.lookup("mamma").display == "Mom"
    assert store.lookup("MOTHER").display == "Mom"


def test_persistence_across_instances(tmp_path):
    EntityStore(tmp_path).learn("Stella", ["Pets", "Dog"])
    reopened = EntityStore(tmp_path)
    assert reopened.full_path("Stella") == ["Pets", "Dog", "Stella"]
    assert "stella" in [n.lower() for n in reopened.names()]


def test_intermediate_nodes_become_known(tmp_path):
    # Teaching a deep path creates the intermediate nodes as reusable entities.
    store = EntityStore(tmp_path)
    store.learn("Phoenix", "Location > USA > Arizona")
    for node in ("Location", "USA", "Arizona", "Phoenix"):
        assert store.lookup(node) is not None
    assert store.full_path("Phoenix") == ["Location", "USA", "Arizona", "Phoenix"]


def test_composition_inherits_ancestry(tmp_path):
    # The headline feature: teach Phoenix once, then Moms House just points at it.
    store = EntityStore(tmp_path)
    store.learn("Phoenix", "Location > USA > Arizona")
    store.learn("Moms House", "Phoenix")
    assert store.full_path("Moms House") == [
        "Location", "USA", "Arizona", "Phoenix", "Moms House",
    ]


def test_reuse_does_not_clobber_existing_parent(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Phoenix", "Location > USA > Arizona")
    # Re-referencing Phoenix as a bare parent must keep its ancestry intact.
    store.learn("Dads House", "Phoenix")
    assert store.resolve_chain("Phoenix") == ["Location", "USA", "Arizona"]
    assert store.full_path("Dads House")[:-1] == store.full_path("Phoenix")


def test_collapse_descendants(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Phoenix", "Location > USA > Arizona")
    store.learn("Moms House", "Phoenix")
    assert store.collapse_descendants(["Phoenix", "Moms House"]) == ["Moms House"]
    assert store.is_ancestor("Phoenix", "Moms House") is True
    assert store.is_ancestor("Moms House", "Phoenix") is False
    # Unrelated entities both survive.
    store.learn("Alice", ["People"])
    assert set(store.collapse_descendants(["Alice", "Moms House"])) == {"Alice", "Moms House"}


def test_names_longest_first_for_greedy_match(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Vivaldi Cafe", ["Location"])
    store.learn("Mom", ["People"])
    names = store.names()
    assert names.index("Vivaldi Cafe") < names.index("Mom")


def test_migration_from_legacy_chain_format(tmp_path):
    # Simulate an entities.json written by the previous (flat-chain) version.
    cache = tmp_path / ".tagassist_cache"
    cache.mkdir()
    (cache / "entities.json").write_text(json.dumps({
        "stella": {"display": "Stella", "chain": ["Pets", "Dog"], "aliases": []},
        "mom": {"display": "Mom", "chain": ["People", "Family"], "aliases": ["mamma"]},
    }))
    store = EntityStore(tmp_path)
    # Full paths preserved, and intermediates now exist as entities.
    assert store.full_path("Stella") == ["Pets", "Dog", "Stella"]
    assert store.full_path("Mom") == ["People", "Family", "Mom"]
    assert store.lookup("Dog") is not None
    assert store.lookup("mamma").display == "Mom"
    # File was rewritten in the new parent-pointer format.
    raw = json.loads((cache / "entities.json").read_text())
    assert raw["stella"]["parent"] == "Dog"
    assert "chain" not in raw["stella"]

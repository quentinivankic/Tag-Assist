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
    assert ent.chain == ["Pets", "Dog"]
    assert ent.as_path() == ["Pets", "Dog", "Stella"]


def test_alias_lookup(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Mom", ["People", "Family"], aliases=("mamma", "mother"))
    assert store.lookup("mamma").display == "Mom"
    assert store.lookup("MOTHER").display == "Mom"


def test_persistence_across_instances(tmp_path):
    EntityStore(tmp_path).learn("Stella", ["Pets", "Dog"])
    reopened = EntityStore(tmp_path)
    assert reopened.lookup("Stella").chain == ["Pets", "Dog"]
    assert "stella" in [n.lower() for n in reopened.names()]


def test_names_longest_first_for_greedy_match(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Vivaldi Cafe", ["Location"])
    store.learn("Mom", ["People"])
    names = store.names()
    # multi-word entity should sort before short ones
    assert names.index("Vivaldi Cafe") < names.index("Mom")


def test_learn_does_not_clobber_chain_when_relearning_alias(tmp_path):
    store = EntityStore(tmp_path)
    store.learn("Mom", ["People", "Family"], aliases=("mamma",))
    store.learn("Mom", ["People", "Family"], aliases=("mum",))
    ent = store.lookup("Mom")
    assert set(ent.aliases) == {"mamma", "mum"}
    assert ent.chain == ["People", "Family"]

from tagassist import interview


def test_parse_people_splits_and_titlecases():
    assert interview.parse_answer("alice and bob", "People") == ["Alice", "Bob"]
    assert interview.parse_answer("Alice, Bob, charlie", "People") == ["Alice", "Bob", "Charlie"]


def test_parse_strips_me_and_filler():
    # "me and Alice at the beach" for People -> just Alice (filler/me dropped)
    assert interview.parse_answer("just me and Alice", "People") == ["Alice"]


def test_parse_location_drops_edge_filler():
    assert interview.parse_answer("at Camelback Mountain", "Location") == ["Camelback Mountain"]
    assert interview.parse_answer("in Phoenix, Arizona", "Location") == ["Phoenix", "Arizona"]


def test_parse_context_keeps_phrases_lowercase():
    assert interview.parse_answer("sunset hike, group trip 2023", "Context") == [
        "sunset hike",
        "group trip 2023",
    ]


def test_empty_answers_yield_nothing():
    for empty in ["", "none", "nobody", "n/a", "idk", "skip"]:
        assert interview.parse_answer(empty, "People") == []


def test_dedup_case_insensitive():
    assert interview.parse_answer("Alice, alice, ALICE", "People") == ["Alice"]


def test_preserves_internal_capitals():
    assert interview.parse_answer("McNally", "People") == ["McNally"]


def test_parse_interview_maps_categories():
    answers = {"people": "Alice", "location": "Phoenix", "context": "birthday"}
    result = interview.parse_interview(answers)
    assert result == {
        "People": ["Alice"],
        "Location": ["Phoenix"],
        "Context": ["birthday"],
    }

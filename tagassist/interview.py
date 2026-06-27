"""Turn plain-English answers into structured tag suggestions.

The interview asks three questions per photo — who / where / context — each
mapped to a TagStudio category. This module converts a free-text answer into a
list of candidate tags. A local LLM can do this better (see ``llm.py``); this
rule-based parser is the always-available fallback and needs no GPU.

The output is always *suggestions* — the UI lets the user confirm/edit before
anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Category each question feeds into. Order = order asked.
QUESTIONS: list[dict[str, str]] = [
    {
        "key": "people",
        "category": "People",
        "prompt": "Who is in this photo?",
        "hint": "e.g. Alice, Bob and my sister",
    },
    {
        "key": "location",
        "category": "Location",
        "prompt": "Where was this taken?",
        "hint": "e.g. Camelback Mountain, Phoenix",
    },
    {
        "key": "context",
        "category": "Context",
        "prompt": "What's happening here?",
        "hint": "e.g. sunset hike, group trip 2023",
    },
]

# Filler words/phrases stripped from answers before splitting into tags.
_FILLER = {
    "the", "a", "an", "and", "with", "at", "in", "on", "of", "to",
    "me", "my", "our", "we", "just", "some", "it", "is", "was", "are",
    "this", "that", "here", "there", "photo", "picture", "pic", "image",
    "taken", "during", "while", "its", "im", "i",
}

# Phrases meaning "nothing / skip".
_EMPTY_ANSWERS = {"", "none", "nobody", "no one", "n/a", "na", "idk",
                  "i don't know", "dont know", "nothing", "skip", "-"}

_SPLIT_RE = re.compile(r"\s*(?:,|;|/|\band\b|&|\+|\bwith\b)\s*", re.IGNORECASE)


@dataclass
class Suggestion:
    category: str
    tags: list[str]


def _clean_phrase(phrase: str, *, titlecase: bool) -> str:
    """Trim a phrase down to a tag: drop leading/trailing filler words."""
    words = [w for w in re.split(r"\s+", phrase.strip()) if w]
    # Drop filler only at the edges, so "trip to the lake" -> "trip to the lake"
    # keeps internal words but "at the lake" -> "lake".
    while words and words[0].lower().strip(".,") in _FILLER:
        words.pop(0)
    while words and words[-1].lower().strip(".,") in _FILLER:
        words.pop()
    cleaned = " ".join(words).strip(" .,-")
    if not cleaned:
        return ""
    if titlecase:
        # Title-case names/places but preserve existing internal capitals
        # (e.g. "McNally" stays "McNally").
        cleaned = " ".join(
            w if any(c.isupper() for c in w[1:]) else w.capitalize()
            for w in cleaned.split()
        )
    return cleaned


def parse_answer(answer: str, category: str) -> list[str]:
    """Parse one free-text answer into a de-duplicated list of tag names.

    People and Location are title-cased (proper nouns); Context is left as the
    user wrote it (lower-ish phrases like "sunset hike").
    """
    if answer is None:
        return []
    normalized = answer.strip().lower()
    if normalized in _EMPTY_ANSWERS:
        return []

    titlecase = category in ("People", "Location")
    parts = _SPLIT_RE.split(answer)
    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = _clean_phrase(part, titlecase=titlecase)
        if not tag:
            continue
        key = tag.lower()
        if key in _EMPTY_ANSWERS or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def parse_interview(answers: dict[str, str]) -> dict[str, list[str]]:
    """Parse a full set of answers keyed by question key into category->tags."""
    out: dict[str, list[str]] = {}
    for q in QUESTIONS:
        ans = answers.get(q["key"], "")
        tags = parse_answer(ans, q["category"])
        if tags:
            out[q["category"]] = tags
    return out

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

# Abbreviations whose trailing period must NOT be treated as a sentence break,
# so "St. Louis" / "Mt. Fuji" / "Dr. Smith" stay intact.
_ABBREV = {"st", "mt", "dr", "mr", "mrs", "ms", "jr", "sr", "ave", "blvd",
           "rd", "ln", "vs", "dept", "no", "ft"}

_SPLIT_RE = re.compile(r"\s*(?:,|;|/|\n|\band\b|&|\+|\bwith\b)\s*", re.IGNORECASE)


def _split_sentences(text: str) -> list[str]:
    """Split on a period that ends a sentence (period + whitespace), but keep
    abbreviations and single-letter initials (e.g. 'D.C.') intact."""
    segments: list[str] = []
    cur = ""
    for part in re.split(r"(\.\s+)", text):
        if re.fullmatch(r"\.\s+", part or ""):
            last = re.search(r"(\w+)\s*$", cur)
            word = last.group(1).lower() if last else ""
            if len(word) <= 1 or word in _ABBREV:
                cur += part  # protected: not a sentence break
            else:
                segments.append(cur)
                cur = ""
        else:
            cur += part or ""
    if cur:
        segments.append(cur)
    return segments


def _extract_known(answer: str, known_names: list[str]) -> tuple[list[str], str]:
    """Pull known entity names out of the raw answer before splitting, so
    multi-word entities (e.g. 'Vivaldi Cafe', 'Mom and Dad') aren't torn apart.

    Returns (matched_names_in_order, remaining_text). ``known_names`` should be
    sorted longest-first so the most specific match wins.
    """
    found: list[str] = []
    remaining = answer
    for name in known_names:
        if not name.strip():
            continue
        pattern = re.compile(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE
        )
        if pattern.search(remaining):
            found.append(name)
            remaining = pattern.sub(" , ", remaining)
    return found, remaining


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


def parse_answer(
    answer: str, category: str, known_names: list[str] | None = None
) -> list[str]:
    """Parse one free-text answer into a de-duplicated list of tag names.

    People and Location are title-cased (proper nouns); Context is left as the
    user wrote it (lower-ish phrases like "sunset hike").

    If ``known_names`` is given (display names + aliases of already-learned
    entities, longest-first), those are pulled out verbatim first so multi-word
    entities survive splitting, and returned with their original casing.
    """
    if answer is None:
        return []
    normalized = answer.strip().lower()
    if normalized in _EMPTY_ANSWERS:
        return []

    titlecase = category in ("People", "Location")
    tags: list[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        key = tag.lower()
        if not tag or key in _EMPTY_ANSWERS or key in seen:
            return
        seen.add(key)
        tags.append(tag)

    remaining = answer
    if known_names:
        matched, remaining = _extract_known(answer, known_names)
        for name in matched:
            _add(name)  # verbatim — known entities keep their stored casing

    for sentence in _split_sentences(remaining):
        for part in _SPLIT_RE.split(sentence):
            _add(_clean_phrase(part, titlecase=titlecase))
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

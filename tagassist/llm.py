"""Optional local-LLM parsing of plain-English answers, via Ollama.

If ``TAGASSIST_OLLAMA_MODEL`` is set and an Ollama server is reachable, we ask
the model to normalize a free-text answer into clean tags (e.g. dedupe, expand
"me and Alice" sensibly, split compound phrases). If anything is unavailable or
fails, we transparently fall back to the rule-based parser in ``interview.py``.

This keeps the LLM strictly optional: v1 runs fully without a GPU or Ollama.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import interview

_OLLAMA_HOST = os.environ.get("TAGASSIST_OLLAMA_HOST", "http://localhost:11434")
_OLLAMA_MODEL = os.environ.get("TAGASSIST_OLLAMA_MODEL", "").strip()

_PROMPT = """You convert a person's casual answer into clean photo tags.
Question asked: {prompt}
Category: {category}
Their answer: "{answer}"

Return ONLY a JSON array of short tag strings (no prose). Rules:
- One tag per distinct person, place, or concept.
- Proper nouns (people, places) in Title Case; concepts lower case.
- Expand "me"/"us" to nothing (the photographer isn't a subject unless named).
- No duplicates, no empty strings. If the answer means "nothing", return [].
"""


def available() -> bool:
    """True if an Ollama model is configured and the server responds."""
    if not _OLLAMA_MODEL:
        return False
    try:
        with urllib.request.urlopen(f"{_OLLAMA_HOST}/api/tags", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _call_ollama(prompt: str, timeout: float) -> str | None:
    payload = json.dumps(
        {"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False}
    ).encode()
    req = urllib.request.Request(
        f"{_OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        return body.get("response")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _extract_json_array(text: str) -> list[str] | None:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [str(x).strip() for x in data if str(x).strip()]


def parse_answer(
    answer: str,
    category: str,
    known_names: list[str] | None = None,
    *,
    timeout: float = 20.0,
) -> list[str]:
    """LLM-normalize an answer into tags, falling back to the rule parser.

    Always returns a usable list; never raises. The fallback guarantees the
    interview works identically with or without a model present. ``known_names``
    is forwarded to the rule parser so learned multi-word entities survive.
    """
    if available():
        prompt_text = _PROMPT.format(
            prompt=next(
                (q["prompt"] for q in interview.QUESTIONS if q["category"] == category),
                "",
            ),
            category=category,
            answer=answer.replace('"', "'"),
        )
        raw = _call_ollama(prompt_text, timeout=timeout)
        if raw:
            tags = _extract_json_array(raw)
            if tags is not None:
                # De-dup case-insensitively, preserve order.
                seen: set[str] = set()
                out: list[str] = []
                for t in tags:
                    if t.lower() not in seen:
                        seen.add(t.lower())
                        out.append(t)
                return out
    return interview.parse_answer(answer, category, known_names)

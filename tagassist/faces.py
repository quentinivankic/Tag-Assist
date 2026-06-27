"""Optional identity learning (face recognition) — interface + safe stub.

The goal: label a face once ("this is Alice") and have Tag-Assist auto-suggest
Alice on future photos. That needs face embeddings (``insightface`` /
``face_recognition``), which are heavyweight optional deps. v1 ships the
*interface* and a learned-name store so the rest of the app can integrate
cleanly; the embedding backend is loaded only if installed.

If no backend is available, ``FaceEngine.available`` is False and the app simply
skips face suggestions — everything else works unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CACHE_DIRNAME = ".tagassist_cache"


@dataclass
class FaceMatch:
    name: str
    confidence: float


class FaceEngine:
    """Pluggable face-recognition front end.

    Backends are detected at construction. Today only the "none" backend is
    wired; ``insightface`` integration is the documented next step. The public
    API (``suggest`` / ``learn``) is intentionally backend-agnostic so enabling
    it later is a drop-in change.
    """

    def __init__(self, library_root: str | Path, entities=None):
        from .entities import EntityStore

        self.cache_dir = Path(library_root) / CACHE_DIRNAME
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._entities = entities if entities is not None else EntityStore(library_root)
        self._backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str:
        try:
            import insightface  # noqa: F401

            return "insightface"
        except Exception:
            return "none"

    @property
    def available(self) -> bool:
        return self._backend != "none"

    @property
    def backend(self) -> str:
        return self._backend

    # -- learned-names store (works regardless of backend) ---------------

    def known_people(self) -> list[str]:
        """Display names of learned entities that are people (chain top == People)."""
        people = [
            e.display
            for e in self._entities.all()
            if e.chain and e.chain[0] == "People"
        ]
        return sorted(people)

    def remember_person(self, name: str) -> None:
        """Record a person for quick reuse, without clobbering a richer chain."""
        name = name.strip()
        if not name:
            return
        if self._entities.lookup(name) is None:
            self._entities.learn(name, ["People"])

    # -- recognition (no-op until a backend is enabled) ------------------

    def suggest(self, image_path: str | Path) -> list[FaceMatch]:
        """Suggest people likely in this photo. Empty until a backend is on."""
        if not self.available:
            return []
        # Backend implementation goes here (embed -> nearest-neighbor lookup).
        return []

    def learn(self, image_path: str | Path, name: str) -> None:
        """Associate the face(s) in this photo with a name."""
        self.remember_person(name)
        if not self.available:
            return
        # Backend implementation goes here (store embedding -> name).

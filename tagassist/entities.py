"""Per-library knowledge base of entities and how they nest.

This is the "learns your world" store. The first time you mention a name the app
doesn't know, it asks what that name *is* (a path like ``Pets > Dog``). From then
on the name is auto-recognized and its full parent chain is applied to photos.

Stored as JSON at ``<library>/.tagassist_cache/entities.json``::

    {
      "stella": {"display": "Stella", "chain": ["Pets", "Dog"], "aliases": []},
      "mom":    {"display": "Mom",    "chain": ["People", "Family"], "aliases": ["mamma"]}
    }

The key is always the lowercased name; ``chain`` is the parent path from broadest
to the direct parent (the entity itself is the leaf appended at tag-write time).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIRNAME = ".tagassist_cache"
STORE_FILENAME = "entities.json"

# Accept "Pets > Dog", "Pets/Dog", "Pets, Dog" or "Pets > Dog" when the user
# describes what something is.
_CHAIN_SPLIT = re.compile(r"\s*(?:>|/|»|->|,)\s*")


@dataclass
class Entity:
    display: str
    chain: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    def as_path(self) -> list[str]:
        """Full top-to-leaf path including the entity itself."""
        return [*self.chain, self.display]


def parse_chain(text: str) -> list[str]:
    """Parse a user-typed parent path like 'Pets > Dog' into ['Pets', 'Dog'].

    Empty/whitespace yields an empty chain (entity has no parent).
    """
    if not text:
        return []
    parts = [p.strip() for p in _CHAIN_SPLIT.split(text) if p.strip()]
    return parts


class EntityStore:
    """Load/save the learned entities for one library."""

    def __init__(self, library_root: str | Path):
        self.cache_dir = Path(library_root) / CACHE_DIRNAME
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.cache_dir / STORE_FILENAME
        self._data: dict[str, Entity] = {}
        self._alias_index: dict[str, str] = {}  # alias(lower) -> key
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for key, val in raw.items():
            ent = Entity(
                display=val.get("display", key),
                chain=list(val.get("chain", [])),
                aliases=list(val.get("aliases", [])),
            )
            self._data[key] = ent
        self._rebuild_alias_index()

    def _rebuild_alias_index(self) -> None:
        self._alias_index = {}
        for key, ent in self._data.items():
            for alias in ent.aliases:
                self._alias_index[alias.lower()] = key

    def _save(self) -> None:
        out = {
            key: {"display": e.display, "chain": e.chain, "aliases": e.aliases}
            for key, e in sorted(self._data.items())
        }
        self.path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- lookups ---------------------------------------------------------

    def lookup(self, name: str) -> Entity | None:
        """Find an entity by name or alias, case-insensitively."""
        key = name.strip().lower()
        if key in self._data:
            return self._data[key]
        if key in self._alias_index:
            return self._data[self._alias_index[key]]
        return None

    def names(self) -> list[str]:
        """All known display names + aliases, for greedy multi-word matching."""
        out: list[str] = []
        for ent in self._data.values():
            out.append(ent.display)
            out.extend(ent.aliases)
        # Longest first so multi-word entities win over their prefixes.
        return sorted(set(out), key=lambda s: (-len(s), s.lower()))

    def all(self) -> list[Entity]:
        return [self._data[k] for k in sorted(self._data)]

    def __contains__(self, name: str) -> bool:
        return self.lookup(name) is not None

    def __len__(self) -> int:
        return len(self._data)

    # -- writes ----------------------------------------------------------

    def learn(
        self, name: str, chain: list[str] | str, aliases: tuple[str, ...] = ()
    ) -> Entity:
        """Record (or update) what ``name`` is and how it nests.

        ``chain`` may be a list (['Pets','Dog']) or a user-typed string
        ('Pets > Dog'). Returns the stored Entity.
        """
        name = name.strip()
        if not name:
            raise ValueError("Entity name must not be empty")
        if isinstance(chain, str):
            chain = parse_chain(chain)
        key = name.lower()
        existing = self._data.get(key)
        merged_aliases = sorted(
            {*(existing.aliases if existing else []), *(a.strip() for a in aliases if a.strip())}
        )
        ent = Entity(display=name, chain=list(chain), aliases=merged_aliases)
        self._data[key] = ent
        self._rebuild_alias_index()
        self._save()
        return ent

    def add_alias(self, name: str, alias: str) -> None:
        ent = self.lookup(name)
        alias = alias.strip()
        if ent is None or not alias:
            return
        if alias.lower() not in (a.lower() for a in ent.aliases):
            ent.aliases.append(alias)
            self._rebuild_alias_index()
            self._save()

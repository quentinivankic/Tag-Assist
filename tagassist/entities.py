"""Per-library knowledge base of entities and how they nest.

This is the "learns your world" store. The first time you mention a name the app
doesn't know, it asks what that name *is* (a path like ``Pets > Dog``). From then
on the name is auto-recognized and its full ancestry is applied to photos.

**Composable hierarchy.** Each entity stores a single ``parent`` (the display
name of another entity), not a flattened chain. Full ancestry is computed by
walking parent links. This is what lets you teach a place's parent *once* and
have everything below it inherit the rest::

    teach Phoenix   -> "Location > USA > Arizona"     (Phoenix.parent = Arizona)
    teach Moms House -> "Phoenix"                      (Moms House.parent = Phoenix)
    full_path("Moms House") == [Location, USA, Arizona, Phoenix, Moms House]

Stored as JSON at ``<library>/.tagassist_cache/entities.json``::

    {
      "phoenix":    {"display": "Phoenix",    "parent": "Arizona", "aliases": []},
      "moms house": {"display": "Moms House", "parent": "Phoenix", "aliases": []}
    }

Legacy records written with a ``chain`` list (an earlier format) are migrated to
parent links automatically on load.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIRNAME = ".tagassist_cache"
STORE_FILENAME = "entities.json"

# Accept "Pets > Dog", "Pets/Dog", "Pets, Dog" or "Pets » Dog" as a parent path.
_CHAIN_SPLIT = re.compile(r"\s*(?:>|/|»|->|,)\s*")


@dataclass
class Entity:
    display: str
    parent: str | None = None  # display name of the parent entity (None = root)
    aliases: list[str] = field(default_factory=list)


def _norm_tight(s: str) -> str:
    """Lowercase and drop everything but letters/digits, so 'camel back
    mountain', 'Camelback Mountain' and "Mom's House"/'moms house' compare
    equal once spacing/punctuation/case are removed."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_chain(text: str) -> list[str]:
    """Parse a user-typed parent path 'Pets > Dog' into ['Pets', 'Dog'] (broad
    to narrow). Empty/whitespace yields an empty list (entity is a root)."""
    if not text:
        return []
    return [p.strip() for p in _CHAIN_SPLIT.split(text) if p.strip()]


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
        legacy: list[tuple[Entity, list[str]]] = []
        for key, val in raw.items():
            ent = Entity(
                display=val.get("display", key),
                parent=val.get("parent"),
                aliases=list(val.get("aliases", [])),
            )
            self._data[key] = ent
            if "parent" not in val and "chain" in val:  # legacy flat-chain record
                legacy.append((ent, list(val.get("chain", []))))
        if legacy:
            for ent, chain in legacy:
                ent.parent = self._link_path(chain)
            self._save()  # rewrite in the new parent-pointer format
        self._rebuild_alias_index()

    def _rebuild_alias_index(self) -> None:
        self._alias_index = {}
        for key, ent in self._data.items():
            for alias in ent.aliases:
                self._alias_index[alias.lower()] = key

    def _save(self) -> None:
        out = {
            key: {"display": e.display, "parent": e.parent, "aliases": e.aliases}
            for key, e in sorted(self._data.items())
        }
        self.path.write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # -- lookups ---------------------------------------------------------

    def lookup(self, name: str) -> Entity | None:
        """Find an entity by name or alias, case-insensitively."""
        key = name.strip().lower()
        if key in self._data:
            return self._data[key]
        if key in self._alias_index:
            return self._data[self._alias_index[key]]
        return None

    def resolve_chain(self, name: str) -> list[str]:
        """Ancestry of ``name`` from broadest root down to its direct parent
        (excludes the entity itself). Cycle-guarded."""
        ent = self.lookup(name)
        chain: list[str] = []
        seen: set[str] = set()
        cur = ent.parent if ent else None
        while cur:
            kl = cur.lower()
            if kl in seen:
                break
            seen.add(kl)
            parent_ent = self._data.get(kl)
            chain.append(parent_ent.display if parent_ent else cur)
            cur = parent_ent.parent if parent_ent else None
        chain.reverse()
        return chain

    def full_path(self, name: str) -> list[str]:
        """Full top-to-leaf path including the entity itself."""
        ent = self.lookup(name)
        display = ent.display if ent else name.strip()
        return [*self.resolve_chain(name), display]

    def is_ancestor(self, a: str, b: str) -> bool:
        """True if ``a`` appears in ``b``'s ancestry."""
        a_ent = self.lookup(a)
        a_disp = (a_ent.display if a_ent else a.strip()).lower()
        return a_disp in [c.lower() for c in self.resolve_chain(b)]

    def collapse_descendants(self, names: list[str]) -> list[str]:
        """Drop any name that is an ancestor of another in the list.

        Mentioning 'Phoenix' and 'Moms House' collapses to ['Moms House'] —
        TagStudio already makes the ancestors searchable via the child.
        """
        canon: list[str] = []
        seen: set[str] = set()
        for n in names:
            ent = self.lookup(n)
            disp = ent.display if ent else n.strip()
            if not disp or disp.lower() in seen:
                continue
            seen.add(disp.lower())
            canon.append(disp)
        return [
            d for d in canon
            if not any(o != d and self.is_ancestor(d, o) for o in canon)
        ]

    def suggest_match(self, name: str, *, cutoff: float = 0.85) -> str | None:
        """Closest known entity for an UNrecognized name, or None.

        Catches spacing/punctuation/case differences ('camel back mountain' vs
        'camelback mountain') exactly, and small typos via edit-distance. The
        caller always confirms with the user, so this only proposes.
        """
        q = _norm_tight(name)
        if len(q) < 3:
            return None
        best: str | None = None
        best_ratio = 0.0
        for ent in self._data.values():
            for cand in (ent.display, *ent.aliases):
                c = _norm_tight(cand)
                if not c:
                    continue
                if c == q:  # same once spacing/punct/case removed
                    return ent.display
                ratio = difflib.SequenceMatcher(None, q, c).ratio()
                if ratio > best_ratio:
                    best_ratio, best = ratio, ent.display
        return best if best_ratio >= cutoff else None

    def names(self) -> list[str]:
        """All known display names + aliases (longest first), for greedy
        multi-word matching and type-ahead."""
        out: list[str] = []
        for ent in self._data.values():
            out.append(ent.display)
            out.extend(ent.aliases)
        return sorted(set(out), key=lambda s: (-len(s), s.lower()))

    def children(self, name: str) -> list[Entity]:
        """Direct children of an entity (those whose parent is ``name``)."""
        ent = self.lookup(name)
        if ent is None:
            return []
        target = ent.display.lower()
        kids = [e for e in self._data.values() if (e.parent or "").lower() == target]
        return sorted(kids, key=lambda e: e.display.lower())

    def all(self) -> list[Entity]:
        return [self._data[k] for k in sorted(self._data)]

    def __contains__(self, name: str) -> bool:
        return self.lookup(name) is not None

    def __len__(self) -> int:
        return len(self._data)

    # -- writes ----------------------------------------------------------

    def _link_path(self, segments: list[str]) -> str | None:
        """Ensure each segment exists as an entity linked to the previous one,
        reusing existing nodes WITHOUT clobbering their parent. Returns the
        display name of the last (narrowest) segment, or None if empty."""
        prev: str | None = None
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            k = seg.lower()
            ent = self._data.get(k)
            if ent is None:
                ent = Entity(display=seg, parent=prev)
                self._data[k] = ent
            elif ent.parent is None and prev is not None:
                ent.parent = prev  # fill a gap; never overwrite an existing parent
            prev = ent.display
        return prev

    def learn(
        self, name: str, path: list[str] | str, aliases: tuple[str, ...] = ()
    ) -> Entity:
        """Record what ``name`` is and how it nests.

        ``path`` is the parent path (broad->narrow), as a list or a typed string
        ('Location > USA > Arizona'). Intermediate nodes are created/reused so
        the ancestry composes. Returns the stored Entity.
        """
        name = name.strip()
        if not name:
            raise ValueError("Entity name must not be empty")
        segments = parse_chain(path) if isinstance(path, str) else list(path)
        parent = self._link_path(segments)
        key = name.lower()
        existing = self._data.get(key)
        merged_aliases = sorted(
            {
                *(existing.aliases if existing else []),
                *(a.strip() for a in aliases if a.strip()),
            }
        )
        ent = Entity(display=name, parent=parent, aliases=merged_aliases)
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

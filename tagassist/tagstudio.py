"""Read/write access to a TagStudio library's SQLite database.

TagStudio v9.5+ stores its entire library as ``.TagStudio/ts_library.sqlite``.
The relevant tables (verified against TagStudio's SQLAlchemy models):

* ``entries``       - one row per file (id, folder_id, path, filename, suffix, dates)
* ``tags``          - tags (id, name, shorthand, is_category, is_hidden, ...)
* ``tag_aliases``   - alternate names for a tag (id, name, tag_id)
* ``tag_entries``   - association: (tag_id, entry_id)
* ``tag_parents``   - hierarchy: (parent_id, child_id)
* ``namespaces``    - (namespace, name) for color groups
* ``versions``      - (key, value)

We deliberately do **not** hard-code the full schema for writes. Column sets
drift between TagStudio releases, so writes introspect the live table with
``PRAGMA table_info`` and supply every NOT NULL column we can, using sensible
defaults. That keeps us compatible across versions instead of brittle.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LIBRARY_DIRNAME = ".TagStudio"
DB_FILENAME = "ts_library.sqlite"

# Separators accepted when a user types a parent path like "Pets > Dog".
_CHAIN_SPLIT = re.compile(r"\s*(?:>|/|»|->|,)\s*")


def parse_chain(text) -> list[str]:
    """Parse a parent path 'Pets > Dog' into ['Pets', 'Dog'] (broad->narrow).

    Accepts a list (returned cleaned) or a string. Empty yields []."""
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [str(p).strip() for p in text if str(p).strip()]
    return [p.strip() for p in _CHAIN_SPLIT.split(text) if p.strip()]


def _norm_tight(s: str) -> str:
    """Lowercase, drop everything but letters/digits — so 'camel back mountain',
    'Camelback Mountain' and "Mom's House"/'moms house' compare equal."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class TagStudioError(RuntimeError):
    """Raised when a library can't be opened or a table is missing."""


@dataclass
class Entry:
    """A file in the TagStudio library."""

    id: int
    path: str  # path relative to the library root, as TagStudio stores it
    filename: str
    library_root: Path
    tags: list[str] = field(default_factory=list)

    @property
    def abs_path(self) -> Path:
        """Best-effort absolute path to the file on disk."""
        p = Path(self.path)
        return p if p.is_absolute() else (self.library_root / p)


def find_library_db(library_root: str | Path) -> Path:
    """Return the path to ts_library.sqlite for a given library root folder.

    ``library_root`` is the folder you opened in TagStudio (the one that
    contains the ``.TagStudio`` directory). We also accept being pointed
    directly at the ``.TagStudio`` folder or the sqlite file itself.
    """
    root = Path(library_root)
    if root.is_file() and root.suffix == ".sqlite":
        return root
    if root.name == LIBRARY_DIRNAME:
        candidate = root / DB_FILENAME
    else:
        candidate = root / LIBRARY_DIRNAME / DB_FILENAME
    if not candidate.exists():
        raise TagStudioError(
            f"No TagStudio database found at {candidate}. "
            "Point TAGASSIST_LIBRARY at the folder you open in TagStudio "
            "(the one containing the .TagStudio directory)."
        )
    return candidate


class TagStudioLibrary:
    """A thin, safe accessor for a TagStudio SQLite library.

    Use as a context manager::

        with TagStudioLibrary("/path/to/library") as lib:
            for entry in lib.entries():
                ...
    """

    # Categories Tag-Assist organizes its tags under. Created lazily.
    CATEGORIES = ("People", "Location", "Context")

    def __init__(self, library_root: str | Path):
        self.library_root = Path(library_root)
        if self.library_root.is_file():
            self.library_root = self.library_root.parent.parent
        elif self.library_root.name == LIBRARY_DIRNAME:
            self.library_root = self.library_root.parent
        self.db_path = find_library_db(library_root)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> "TagStudioLibrary":
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._require_tables()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "TagStudioLibrary":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TagStudioError("Library is not connected; call connect() first.")
        return self._conn

    # -- schema introspection -------------------------------------------

    def _tables(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}

    def _require_tables(self) -> None:
        needed = {"entries", "tags", "tag_entries"}
        missing = needed - self._tables()
        if missing:
            raise TagStudioError(
                f"{self.db_path} is missing expected TagStudio tables: "
                f"{sorted(missing)}. Is this a TagStudio v9.5+ library?"
            )

    def _columns(self, table: str) -> dict[str, sqlite3.Row]:
        """Map column name -> PRAGMA row (name, type, notnull, dflt_value, pk)."""
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"]: r for r in rows}

    def _insert_with_defaults(
        self, table: str, values: dict[str, object]
    ) -> int:
        """INSERT a row, auto-filling any NOT NULL column we weren't given.

        This is what makes writes resilient across TagStudio versions: we only
        *need* to know the columns we care about; any other required column gets
        a type-appropriate placeholder so the insert doesn't fail.
        """
        cols = self._columns(table)
        row = dict(values)
        for name, meta in cols.items():
            if name in row:
                continue
            if meta["pk"]:  # autoincrement / handled by SQLite
                continue
            if meta["notnull"] and meta["dflt_value"] is None:
                row[name] = self._default_for(str(meta["type"]))
        placeholders = ", ".join("?" for _ in row)
        col_sql = ", ".join(row)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
            list(row.values()),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _default_for(sqltype: str) -> object:
        t = sqltype.upper()
        if any(k in t for k in ("INT", "BOOL")):
            return 0
        if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUMERIC")):
            return 0
        return ""  # TEXT and everything else

    # -- reads -----------------------------------------------------------

    def entry_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def _entry_order(self) -> str:
        """ORDER BY clause for a chronological feed (newest first, like Immich),
        using whichever date columns this TagStudio version has; falls back to
        filename. Nulls sort last."""
        cols = self._columns("entries")
        date_cols = [c for c in ("date_created", "date_added", "date_modified") if c in cols]
        if not date_cols:
            return "ORDER BY id"
        coalesce = "COALESCE(" + ", ".join(date_cols) + ")"
        return f"ORDER BY {coalesce} IS NULL, {coalesce} DESC, filename DESC, id DESC"

    def entries(self, limit: int | None = None, offset: int = 0) -> list[Entry]:
        sql = f"SELECT id, path, filename FROM entries {self._entry_order()}"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        out: list[Entry] = []
        for r in self.conn.execute(sql).fetchall():
            out.append(
                Entry(
                    id=r["id"],
                    path=str(r["path"]),
                    filename=r["filename"],
                    library_root=self.library_root,
                    tags=self.tags_for_entry(r["id"]),
                )
            )
        return out

    def get_entry(self, entry_id: int) -> Entry | None:
        r = self.conn.execute(
            "SELECT id, path, filename FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if r is None:
            return None
        return Entry(
            id=r["id"],
            path=str(r["path"]),
            filename=r["filename"],
            library_root=self.library_root,
            tags=self.tags_for_entry(r["id"]),
        )

    def tags_for_entry(self, entry_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT t.name AS name
            FROM tag_entries te
            JOIN tags t ON t.id = te.tag_id
            WHERE te.entry_id = ?
            ORDER BY t.name
            """,
            (entry_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def all_tag_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def tags_under_category(self, category: str) -> list[str]:
        """Return names of tags that are children of the given category tag."""
        if "tag_parents" not in self._tables():
            return []
        rows = self.conn.execute(
            """
            SELECT c.name AS name
            FROM tag_parents tp
            JOIN tags p ON p.id = tp.parent_id
            JOIN tags c ON c.id = tp.child_id
            WHERE p.name = ?
            ORDER BY c.name
            """,
            (category,),
        ).fetchall()
        return [r["name"] for r in rows]

    # -- writes ----------------------------------------------------------

    def find_tag(self, name: str) -> int | None:
        """Find a tag id by exact name (case-insensitive), or by alias."""
        r = self.conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if r is not None:
            return r["id"]
        if "tag_aliases" in self._tables():
            r = self.conn.execute(
                "SELECT tag_id FROM tag_aliases WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if r is not None:
                return r["tag_id"]
        return None

    def get_or_create_tag(
        self,
        name: str,
        *,
        is_category: bool = False,
        parent: str | None = None,
    ) -> int:
        """Return the id of a tag with this name, creating it if needed.

        If ``parent`` is given (a category name), the tag is linked under it via
        ``tag_parents`` so it shows up neatly grouped inside TagStudio.
        """
        name = name.strip()
        if not name:
            raise ValueError("Tag name must not be empty")
        tag_id = self.find_tag(name)
        if tag_id is None:
            tag_id = self._insert_with_defaults(
                "tags",
                {
                    "name": name,
                    "is_category": 1 if is_category else 0,
                    "is_hidden": 0,
                },
            )
        if parent:
            parent_id = self.get_or_create_tag(parent, is_category=True)
            self._link_parent(parent_id, tag_id)
        return tag_id

    def _link_parent(self, parent_id: int, child_id: int) -> None:
        if "tag_parents" not in self._tables() or parent_id == child_id:
            return
        exists = self.conn.execute(
            "SELECT 1 FROM tag_parents WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        ).fetchone()
        if exists is None:
            self._insert_with_defaults(
                "tag_parents", {"parent_id": parent_id, "child_id": child_id}
            )

    def add_alias(self, tag_id: int, alias: str) -> None:
        alias = alias.strip()
        if not alias or "tag_aliases" not in self._tables():
            return
        exists = self.conn.execute(
            "SELECT 1 FROM tag_aliases WHERE tag_id = ? AND name = ? COLLATE NOCASE",
            (tag_id, alias),
        ).fetchone()
        if exists is None:
            self._insert_with_defaults(
                "tag_aliases", {"name": alias, "tag_id": tag_id}
            )

    def tag_entry(self, entry_id: int, tag_id: int) -> bool:
        """Associate a tag with an entry. Returns True if newly added."""
        exists = self.conn.execute(
            "SELECT 1 FROM tag_entries WHERE entry_id = ? AND tag_id = ?",
            (entry_id, tag_id),
        ).fetchone()
        if exists is not None:
            return False
        self._insert_with_defaults(
            "tag_entries", {"entry_id": entry_id, "tag_id": tag_id}
        )
        return True

    def apply_entity(
        self, entry_id: int, leaf_name: str, chain: list[str]
    ) -> bool:
        """Attach a learned entity to an entry, building its nested hierarchy.

        ``chain`` is the parent path from broadest to direct parent
        (e.g. ["Pets", "Dog"]); ``leaf_name`` is the entity itself ("Stella").
        We create/reuse each tag, link consecutive pairs in ``tag_parents``
        (Pets->Dog->Stella), and attach ONLY the leaf to the entry — TagStudio
        inherits parent tags in search, so the nesting comes for free.

        Returns True if the leaf was newly attached to this entry. Commits on
        success; rolls back on failure.
        """
        try:
            leaf_id = self._ensure_nested(leaf_name, chain)
            newly = self.tag_entry(entry_id, leaf_id)
            self.conn.commit()
            return newly
        except Exception:
            self.conn.rollback()
            raise

    def _ensure_nested(self, leaf_name: str, chain: list[str]) -> int:
        """Create/reuse ``leaf_name`` nested under ``chain`` (broad->narrow),
        linking each consecutive pair in ``tag_parents``. Returns the leaf's id.
        Does NOT commit — the caller commits."""
        parent_id: int | None = None
        for level in chain:
            level = level.strip()
            if not level:
                continue
            level_id = self.get_or_create_tag(level, is_category=True)
            if parent_id is not None:
                self._link_parent(parent_id, level_id)
            parent_id = level_id
        leaf_id = self.get_or_create_tag(leaf_name)
        if parent_id is not None:
            self._link_parent(parent_id, leaf_id)
        return leaf_id

    # -- hierarchy: TagStudio's DB is the single source of truth ----------
    # These compute nesting straight from the `tags` / `tag_parents` tables,
    # so the app and TagStudio can never disagree.

    def canonical_name(self, name: str) -> str | None:
        """The stored tag name for ``name`` (resolving aliases/case), or None."""
        tid = self.find_tag(name)
        if tid is None:
            return None
        r = self.conn.execute("SELECT name FROM tags WHERE id = ?", (tid,)).fetchone()
        return r["name"] if r else None

    def is_known(self, name: str) -> bool:
        return self.find_tag(name) is not None

    def _parents(self, tag_id: int) -> list[tuple[int, str]]:
        if "tag_parents" not in self._tables():
            return []
        rows = self.conn.execute(
            """SELECT p.id AS id, p.name AS name
               FROM tag_parents tp JOIN tags p ON p.id = tp.parent_id
               WHERE tp.child_id = ?""",
            (tag_id,),
        ).fetchall()
        return [(r["id"], r["name"]) for r in rows]

    def _depth(self, tag_id: int, seen: set[int] | None = None) -> int:
        """Number of ancestors above a tag (cycle-guarded)."""
        seen = seen if seen is not None else set()
        if tag_id in seen:
            return 0
        seen.add(tag_id)
        parents = self._parents(tag_id)
        if not parents:
            return 0
        return 1 + max(self._depth(pid, seen) for pid, _ in parents)

    def resolve_chain(self, name: str) -> list[str]:
        """Ancestry of a tag from broadest root to its direct parent, read from
        ``tag_parents`` (excludes the tag itself).

        If a tag has more than one parent (which can happen from earlier buggy
        writes), the **deepest** parent is chosen at each step — the most
        specific nesting — so e.g. Phoenix resolves through Arizona, not a stray
        direct USA link. Cycle-guarded.
        """
        tid = self.find_tag(name)
        if tid is None:
            return []
        chain: list[str] = []
        seen: set[int] = set()
        cur = tid
        while True:
            parents = [(pid, pname) for pid, pname in self._parents(cur) if pid not in seen]
            if not parents:
                break
            pid, pname = max(parents, key=lambda p: self._depth(p[0]))
            seen.add(pid)
            chain.append(pname)
            cur = pid
        chain.reverse()
        return chain

    def full_path(self, name: str) -> list[str]:
        c = self.canonical_name(name)
        if c is None:
            return [name.strip()]
        return [*self.resolve_chain(name), c]

    def children(self, name: str) -> list[str]:
        """Direct children of a tag (names), via ``tag_parents``."""
        c = self.canonical_name(name)
        return self.tags_under_category(c) if c else []

    def _children_ids(self, tag_id: int) -> list[tuple[int, str]]:
        if "tag_parents" not in self._tables():
            return []
        rows = self.conn.execute(
            """SELECT c.id AS id, c.name AS name
               FROM tag_parents tp JOIN tags c ON c.id = tp.child_id
               WHERE tp.parent_id = ?""",
            (tag_id,),
        ).fetchall()
        return [(r["id"], r["name"]) for r in rows]

    def descendants(self, name: str) -> list[str]:
        """All descendant tag names below ``name`` (any depth). Cycle-guarded."""
        root = self.find_tag(name)
        if root is None:
            return []
        out: list[str] = []
        seen: set[int] = {root}
        stack = [root]
        while stack:
            for cid, cname in self._children_ids(stack.pop()):
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(cname)
                stack.append(cid)
        return out

    def leaf_descendants(self, name: str) -> list[str]:
        """Descendants that are themselves leaves (no children) — i.e. the
        specific 'bottom' places/things under a location like Arizona."""
        root = self.find_tag(name)
        if root is None:
            return []
        leaves: list[str] = []
        seen: set[int] = {root}
        stack = [root]
        while stack:
            for cid, cname in self._children_ids(stack.pop()):
                if cid in seen:
                    continue
                seen.add(cid)
                stack.append(cid)
                if not self._children_ids(cid):
                    leaves.append(cname)
        return sorted(set(leaves), key=str.lower)

    def tag_tree(self) -> list[dict]:
        """The full hierarchy as nested {name, children} dicts, roots first.

        Roots are tags with no parent. A multi-parent tag appears under each of
        its parents. Cycle-guarded."""
        id2name = {
            r["id"]: r["name"]
            for r in self.conn.execute("SELECT id, name FROM tags").fetchall()
        }
        children_map: dict[int, list[int]] = {}
        has_parent: set[int] = set()
        if "tag_parents" in self._tables():
            for r in self.conn.execute(
                "SELECT parent_id, child_id FROM tag_parents"
            ).fetchall():
                children_map.setdefault(r["parent_id"], []).append(r["child_id"])
                has_parent.add(r["child_id"])

        def build(tid: int, seen: frozenset[int]) -> dict:
            name = id2name.get(tid, "?")
            if tid in seen:
                return {"name": name, "children": []}
            seen = seen | {tid}
            kids = sorted(
                children_map.get(tid, []), key=lambda i: id2name.get(i, "").lower()
            )
            return {"name": name, "children": [build(k, seen) for k in kids]}

        roots = [i for i in id2name if i not in has_parent]
        return [build(r, frozenset()) for r in sorted(roots, key=lambda i: id2name[i].lower())]

    def is_ancestor(self, a: str, b: str) -> bool:
        ca = self.canonical_name(a)
        if ca is None:
            return False
        return ca.lower() in [x.lower() for x in self.resolve_chain(b)]

    def collapse_descendants(self, names: list[str]) -> list[str]:
        """Drop any name that is an ancestor of another in the list (keep the
        deepest). TagStudio makes the ancestors searchable via the child."""
        canon: list[str] = []
        seen: set[str] = set()
        for n in names:
            disp = self.canonical_name(n) or n.strip()
            if disp and disp.lower() not in seen:
                seen.add(disp.lower())
                canon.append(disp)
        return [
            d for d in canon
            if not any(o != d and self.is_ancestor(d, o) for o in canon)
        ]

    def known_names(self) -> list[str]:
        """All tag names + aliases (longest first), for greedy matching and
        type-ahead."""
        names = list(self.all_tag_names())
        if "tag_aliases" in self._tables():
            names += [
                r["name"]
                for r in self.conn.execute("SELECT name FROM tag_aliases").fetchall()
            ]
        return sorted(set(names), key=lambda s: (-len(s), s.lower()))

    def suggest_match(self, name: str, *, cutoff: float = 0.85) -> str | None:
        """Closest existing tag for an unrecognized name (spacing/case/typo),
        or None. The caller always confirms, so this only proposes."""
        q = _norm_tight(name)
        if len(q) < 3:
            return None
        best: str | None = None
        best_ratio = 0.0
        for cand in self.known_names():
            c = _norm_tight(cand)
            if not c:
                continue
            if c == q:
                return self.canonical_name(cand)
            ratio = difflib.SequenceMatcher(None, q, c).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, cand
        return self.canonical_name(best) if best and best_ratio >= cutoff else None

    def learn(self, name: str, path) -> str:
        """Create the nested tag hierarchy in the DB (without attaching to any
        photo). ``path`` is broad->narrow (list or 'Pets > Dog' string). Returns
        the canonical leaf name. Commits on success."""
        chain = parse_chain(path)
        try:
            self._ensure_nested(name, chain)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.canonical_name(name) or name.strip()

    def add_alias_by_name(self, target: str, alias: str) -> bool:
        """Add ``alias`` as an alternate name for the tag ``target``. Commits."""
        tid = self.find_tag(target)
        if tid is None:
            return False
        self.add_alias(tid, alias)
        self.conn.commit()
        return True

    def import_legacy_entities(self) -> int:
        """One-time migration: fold a legacy ``entities.json`` knowledge file
        into the DB (tags, parent links, aliases), then mark it imported so the
        DB is the sole source of truth going forward. Returns count imported."""
        cache = self.library_root / ".tagassist_cache"
        src = cache / "entities.json"
        if not src.exists():
            return 0
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            return 0
        # Tags that are someone's parent should be categories (for grouping).
        parents = {
            (v.get("parent") or "").lower()
            for v in raw.values() if v.get("parent")
        }
        count = 0
        try:
            for key, v in raw.items():
                display = v.get("display", key)
                is_cat = display.lower() in parents
                tid = self.get_or_create_tag(display, is_category=is_cat)
                parent = v.get("parent")
                if parent:
                    pid = self.get_or_create_tag(parent, is_category=True)
                    self._link_parent(pid, tid)
                for alias in v.get("aliases", []) or []:
                    self.add_alias(tid, alias)
                count += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        src.rename(cache / "entities.json.imported")
        return count

    def apply_tags(
        self, entry_id: int, tags_by_category: dict[str, list[str]]
    ) -> list[str]:
        """Create + attach tags to an entry, grouped under categories.

        ``tags_by_category`` maps a category name (e.g. "People") to a list of
        tag names. Returns the list of tag names that were newly attached.
        Commits on success; rolls back on failure.
        """
        added: list[str] = []
        try:
            for category, names in tags_by_category.items():
                cat = category if category in self.CATEGORIES else None
                for name in names:
                    name = name.strip()
                    if not name:
                        continue
                    tag_id = self.get_or_create_tag(name, parent=cat)
                    if self.tag_entry(entry_id, tag_id):
                        added.append(name)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return added

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

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LIBRARY_DIRNAME = ".TagStudio"
DB_FILENAME = "ts_library.sqlite"


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

    def entries(self, limit: int | None = None, offset: int = 0) -> list[Entry]:
        sql = "SELECT id, path, filename FROM entries ORDER BY id"
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

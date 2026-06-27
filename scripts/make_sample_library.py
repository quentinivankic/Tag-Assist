#!/usr/bin/env python3
"""Generate a sample TagStudio-format library to try Tag-Assist on.

Creates ``<root>/.TagStudio/ts_library.sqlite`` plus a handful of generated
JPEG photos registered as entries, with a couple of starter tags. The schema
mirrors the tables Tag-Assist reads/writes (entries, tags, tag_aliases,
tag_entries, tag_parents, namespaces, versions).

Usage::

    python scripts/make_sample_library.py /tmp/sample-lib
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# --- minimal TagStudio-compatible schema -------------------------------------
# Mirrors the columns Tag-Assist relies on. Real TagStudio libraries have more
# columns; our writers introspect and fill those, so this subset is enough to
# develop and test against.
SCHEMA = """
CREATE TABLE folders (
    id      INTEGER PRIMARY KEY,
    path    TEXT UNIQUE,
    uuid    TEXT UNIQUE
);
CREATE TABLE entries (
    id           INTEGER PRIMARY KEY,
    folder_id    INTEGER NOT NULL,
    path         TEXT UNIQUE NOT NULL,
    filename     TEXT NOT NULL,
    suffix       TEXT NOT NULL,
    date_created TEXT,
    date_modified TEXT,
    date_added   TEXT
);
CREATE TABLE namespaces (
    namespace TEXT PRIMARY KEY NOT NULL,
    name      TEXT NOT NULL
);
CREATE TABLE tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    shorthand       TEXT,
    color_namespace TEXT,
    color_slug      TEXT,
    is_category     BOOLEAN NOT NULL DEFAULT 0,
    is_hidden       BOOLEAN NOT NULL DEFAULT 0,
    icon            TEXT,
    disambiguation_id INTEGER
);
CREATE TABLE tag_aliases (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    FOREIGN KEY (tag_id) REFERENCES tags(id)
);
CREATE TABLE tag_entries (
    tag_id   INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    FOREIGN KEY (tag_id) REFERENCES tags(id),
    FOREIGN KEY (entry_id) REFERENCES entries(id)
);
CREATE TABLE tag_parents (
    parent_id INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES tags(id),
    FOREIGN KEY (child_id) REFERENCES tags(id)
);
CREATE TABLE versions (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""

SAMPLE_PHOTOS = [
    ("beach_sunset.jpg", (255, 170, 90), "Beach at sunset"),
    ("birthday_party.jpg", (250, 220, 120), "Birthday party"),
    ("mountain_hike.jpg", (130, 180, 150), "Mountain hike"),
    ("city_dinner.jpg", (120, 130, 200), "City dinner"),
    ("dog_park.jpg", (170, 200, 120), "Dog at the park"),
]


def make_photo(path: Path, color: tuple[int, int, int], label: str) -> None:
    img = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 620, 460], outline=(255, 255, 255), width=3)
    draw.text((40, 220), label, fill=(40, 40, 40))
    img.save(path, "JPEG", quality=85)


def main(root_arg: str) -> None:
    root = Path(root_arg).resolve()
    photos_dir = root / "photos"
    ts_dir = root / ".TagStudio"
    photos_dir.mkdir(parents=True, exist_ok=True)
    ts_dir.mkdir(parents=True, exist_ok=True)

    db_path = ts_dir / "ts_library.sqlite"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    conn.execute("INSERT INTO folders (id, path, uuid) VALUES (1, ?, ?)",
                 (str(root), "sample-folder-uuid"))
    conn.execute("INSERT INTO versions (key, value) VALUES ('SCHEMA_VERSION', 9)")
    conn.execute("INSERT INTO namespaces (namespace, name) VALUES ('default', 'Default')")

    for name, color, label in SAMPLE_PHOTOS:
        photo_path = photos_dir / name
        make_photo(photo_path, color, label)
        rel = photo_path.relative_to(root)
        conn.execute(
            "INSERT INTO entries (folder_id, path, filename, suffix) VALUES (1, ?, ?, ?)",
            (str(rel), name, "jpg"),
        )

    conn.commit()
    conn.close()

    print(f"Sample library created at: {root}")
    print(f"  database: {db_path}")
    print(f"  photos:   {photos_dir} ({len(SAMPLE_PHOTOS)} images)")
    print()
    print("Try it:")
    print(f"  export TAGASSIST_LIBRARY={root}")
    print("  python -m tagassist")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/make_sample_library.py <library-root>")
        raise SystemExit(2)
    main(sys.argv[1])

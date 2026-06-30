#!/usr/bin/env python3
"""Dump the library's tag tree and per-photo tags to a text file.

Usage:
    python scripts/dump_tags.py [LIBRARY_ROOT] [OUTPUT_FILE]

Defaults: ``LIBRARY_ROOT = $TAGASSIST_LIBRARY`` (or prompt if unset);
``OUTPUT_FILE = my_tags.txt`` in the current folder.

The dump iterates tags by id (not name), so duplicate-name tags appear as
distinct rows — exactly what you need to spot the bad-state cases.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python scripts/dump_tags.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tagassist.tagstudio import TagStudioLibrary  # noqa: E402


def main() -> None:
    library = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TAGASSIST_LIBRARY")
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else "my_tags.txt")
    if not library:
        sys.exit(
            "Usage: python scripts/dump_tags.py [LIBRARY_ROOT] [OUTPUT_FILE]\n"
            "Or set TAGASSIST_LIBRARY first."
        )

    lines: list[str] = []
    with TagStudioLibrary(library) as lib:
        dups = lib.duplicate_name_tags()
        dup_ids = {i for ids in dups.values() for i in ids}

        lines.append("== TAG TREE (id, full path) ==")
        for tid, _name, path in lib.all_tag_paths():
            marker = "  <-- DUPLICATE NAME" if tid in dup_ids else ""
            lines.append(f"  id={tid:>3}  {' > '.join(path)}{marker}")

        lines.append("")
        lines.append("== DUPLICATES ==")
        if dups:
            for name, ids in dups.items():
                lines.append(f"  {name}: ids {ids}")
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("== PHOTOS ==")
        for e in lib.entries():
            lines.append(f"  {e.filename}")
            if not e.tags:
                lines.append("      (untagged)")
            for t in e.tags:
                lines.append(f"      {' > '.join(lib.full_path(t))}")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(lines)} lines -> {out_path.resolve()}")


if __name__ == "__main__":
    main()

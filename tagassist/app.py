"""FastAPI web app: the interview UI.

Flow:
1.  ``GET /``            - redirect to the first untagged (or first) entry.
2.  ``GET /photo/{id}``  - show the photo, pre-filled hints, the 3 questions.
3.  ``POST /preview``    - parse plain-English answers into tag suggestions
                           (LLM if available, else rule-based) without writing.
4.  ``POST /commit``     - write confirmed tags into the TagStudio library.
5.  ``GET /image/{id}``  - serve the photo bytes.

The tag hierarchy is read and written through TagStudio's own database
(``tags`` / ``tag_parents`` / ``tag_aliases``) — that database is the single
source of truth, so the app and TagStudio can never drift out of sync.
"""

from __future__ import annotations

import json
import shutil
import time
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import exif, geocode, interview, llm
from .config import Config
from .faces import FaceEngine
from .tagstudio import TagStudioLibrary

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Suffixes browsers display natively; everything else is converted to JPEG.
_WEB_SAFE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# Which top-level category each interview box suggests for *new* entities.
_BOX_CATEGORY = {
    "people": "People",
    "location": "Location",
    "context": "Context",
    "phototype": "Type",
}


def _backup_on_startup(library_root: str, *, keep: int = 10) -> None:
    """Copy ts_library.sqlite to a timestamped .bak file before the app does
    any writes. Keeps the most recent ``keep`` backups; older ones are pruned.

    The backup is written next to the live DB inside ``.TagStudio``. Restore is
    just: stop the app, copy the .bak you want back over ts_library.sqlite.
    """
    try:
        from .tagstudio import find_library_db  # local import to avoid cycle
        db = find_library_db(library_root)
    except Exception:
        return  # no library yet (fresh setup); nothing to back up
    backups_dir = db.parent / "tagassist_backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"ts_library.{stamp}.sqlite.bak"
    try:
        shutil.copy2(db, target)
        print(f"[Tag-Assist] backup -> {target}")
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[Tag-Assist] backup FAILED ({exc}); proceeding without snapshot")
        return
    # Prune: keep most-recent ``keep`` .bak files.
    backups = sorted(backups_dir.glob("ts_library.*.sqlite.bak"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except Exception:
            pass


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Tag-Assist")
    faces = FaceEngine(config.library)

    def open_lib() -> TagStudioLibrary:
        return TagStudioLibrary(config.library).connect()

    # Safety: snapshot ts_library.sqlite on startup. Tag-Assist never writes
    # anywhere else, but a per-session backup is a cheap insurance policy when
    # pointing at a real library. Keeps the most recent N backups.
    _backup_on_startup(config.library, keep=10)

    # One-time: fold any legacy entities.json knowledge file into the DB, so
    # TagStudio's database becomes the sole source of truth from here on.
    try:
        with open_lib() as _lib:
            migrated = _lib.import_legacy_entities()
            if migrated:
                print(f"[Tag-Assist] migrated {migrated} legacy entities into the library DB")
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[Tag-Assist] legacy migration skipped: {exc}")

    def build_geo(lib: TagStudioLibrary, meta) -> dict | None:
        """Reverse-geocode a photo's GPS into clickable location options.

        Each option is either ``known`` (an existing tag -> click applies its
        full chain) or ``unknown`` (a geocoded place name -> click opens a teach
        card pre-filled with the geographic path). Children of a matched city
        (e.g. Phoenix -> Moms House) come first, since GPS gets you to the city
        but only you know the exact spot.
        """
        if not meta.has_gps:
            return None
        g = geocode.reverse(*meta.gps)
        if not g:
            return None
        city, state, country = g["city"], g["state"], g["country"]
        label = ", ".join(x for x in (city, state) if x) or "location"
        options: list[dict] = []
        seen: set[str] = set()

        def add_known(name: str) -> None:
            disp = lib.canonical_name(name)
            if disp and disp.lower() not in seen:
                seen.add(disp.lower())
                options.append({
                    "name": disp, "status": "known", "chain": lib.resolve_chain(disp),
                })

        # 1. All specific spots within the geocoded area: take the broadest
        #    KNOWN location node (state preferred, so you see every place tagged
        #    in Arizona, not just the exact city) and list its leaf descendants.
        anchor = next((c for c in (state, city) if c and lib.is_known(c)), None)
        if anchor:
            for leaf in lib.leaf_descendants(anchor):
                if leaf.lower() not in seen:
                    seen.add(leaf.lower())
                    options.append({
                        "name": leaf, "status": "known", "chain": lib.resolve_chain(leaf),
                    })

        # 2. The city / state nodes themselves: apply if known, else offer to teach.
        for cand, parents in ((city, ("Location", country, state)), (state, ("Location", country))):
            if not cand or cand.lower() in seen:
                continue
            if lib.is_known(cand):
                add_known(cand)
            else:
                seen.add(cand.lower())
                options.append({
                    "name": cand, "status": "unknown",
                    "suggested_path": " > ".join(p for p in parents if p),
                })
        return {"label": label, "options": options}

    # -- navigation ------------------------------------------------------

    def _next_untagged_id(lib: TagStudioLibrary) -> int | None:
        for entry in lib.entries():
            if not entry.tags:
                return entry.id
        first = lib.entries(limit=1)
        return first[0].id if first else None

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open_lib() as lib:
            target = _next_untagged_id(lib)
        if target is None:
            return HTMLResponse("<h1>No entries in this library.</h1>")
        return RedirectResponse(url=f"/photo/{target}")

    @app.get("/photo/{entry_id}", response_class=HTMLResponse)
    def photo(request: Request, entry_id: int):
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None:
                raise HTTPException(404, "Entry not found")
            feed = lib.entries()
            all_ids = [e.id for e in feed]
            cur_pos = next((k for k, e in enumerate(feed) if e.id == entry_id), 0)
            # Filmstrip: current photo + the next ~49 (feed order) for bulk tagging.
            strip = [
                {"id": e.id, "filename": e.filename, "tagged": bool(e.tags)}
                for e in feed[cur_pos:cur_pos + 50]
            ]
            existing = entry.tags
            meta = exif.read_meta(entry.abs_path) if entry.abs_path.exists() else exif.PhotoMeta()
            geo = build_geo(lib, meta)
            people_hint = lib.tags_under_category("People")
            face_suggestions = [m.name for m in faces.suggest(entry.abs_path)] if entry.abs_path.exists() else []
            known_nodes = lib.known_names()

        idx = all_ids.index(entry_id) if entry_id in all_ids else 0
        prev_id = all_ids[idx - 1] if idx > 0 else None
        next_id = all_ids[idx + 1] if idx < len(all_ids) - 1 else None

        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "entry": entry,
                "questions": interview.QUESTIONS,
                "existing_tags": existing,
                "meta": meta,
                "geo": geo,
                "people_hint": people_hint,
                "face_suggestions": face_suggestions,
                "known_nodes": known_nodes,
                "strip": strip,
                "position": idx + 1,
                "total": len(all_ids),
                "prev_id": prev_id,
                "next_id": next_id,
                "llm_on": llm.available(),
                "faces_on": faces.available,
            },
        )

    @app.get("/image/{entry_id}")
    def image(entry_id: int):
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None or not entry.abs_path.exists():
                raise HTTPException(404, "Image not found")
            path = entry.abs_path
        if path.suffix.lower() in _WEB_SAFE:
            return FileResponse(path)
        # HEIC/HEIF/TIFF/etc. -> convert to JPEG on the fly for display.
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((1600, 1600))  # cap size; iPhone HEIC are huge
                buf = BytesIO()
                img.save(buf, "JPEG", quality=85)
            return Response(content=buf.getvalue(), media_type="image/jpeg")
        except Exception:
            return FileResponse(path)  # last resort: let the browser try

    @app.get("/thumb/{entry_id}")
    def thumb(entry_id: int):
        """Small JPEG for the bulk-tag filmstrip (any source format)."""
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None or not entry.abs_path.exists():
                raise HTTPException(404, "Image not found")
            path = entry.abs_path
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((220, 220))
                buf = BytesIO()
                img.save(buf, "JPEG", quality=78)
            return Response(content=buf.getvalue(), media_type="image/jpeg")
        except Exception:
            raise HTTPException(404, "Cannot render thumbnail")

    @app.get("/tags", response_class=HTMLResponse)
    def tags(request: Request):
        """A browsable view of the whole tag hierarchy."""
        with open_lib() as lib:
            tree = lib.tag_tree()
            count = len(lib.all_tag_names())
            known_nodes = lib.known_names()
            dup_ids = {i for ids in lib.duplicate_name_tags().values() for i in ids}
        return _TEMPLATES.TemplateResponse(
            request, "tags.html",
            {
                "tree": tree, "count": count,
                "known_nodes": known_nodes, "dup_ids": list(dup_ids),
            },
        )

    @app.post("/tag/create")
    def tag_create(name: str = Form(...), chain: str = Form("")):
        """Create a new tag, optionally nested under a path like 'Pets > Dog'.
        Refuses if the name already exists — use rename/merge for that case."""
        name = name.strip()
        if not name:
            raise HTTPException(400, "Tag name can't be empty")
        with open_lib() as lib:
            if lib.is_known(name):
                raise HTTPException(
                    400,
                    f"A tag named '{name}' already exists. Use rename or merge "
                    "instead.",
                )
            leaf = lib.learn(name, chain)
            out = {"name": leaf, "chain": lib.resolve_chain(leaf)}
        return JSONResponse(out)

    @app.post("/tag/rename")
    def tag_rename(tag_id: int = Form(...), name: str = Form(...)):
        with open_lib() as lib:
            try:
                lib.rename_tag(tag_id, name)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return JSONResponse({"ok": True})

    @app.post("/tag/reparent")
    def tag_reparent(tag_id: int = Form(...), parent_id: str = Form("")):
        pid = int(parent_id) if parent_id.strip() else None  # blank = make root
        with open_lib() as lib:
            try:
                lib.set_parent(tag_id, pid)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return JSONResponse({"ok": True})

    @app.post("/tag/merge")
    def tag_merge(
        source_id: int = Form(...),
        target_id: str = Form(""),
        target_name: str = Form(""),
    ):
        """Merge ``source_id`` into another tag. Prefer ``target_id`` (passed by
        the drag-onto-twin case where the exact tag is known); fall back to
        ``target_name`` for the type-it-in case (ambiguous if duplicates)."""
        with open_lib() as lib:
            if target_id.strip():
                tid = int(target_id)
            else:
                # Resolve by typed name. If multiple tags share the name (the
                # bad-duplicate case), prefer one that ISN'T the source.
                rows = lib.conn.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (target_name,)
                ).fetchall()
                others = [r["id"] for r in rows if r["id"] != source_id]
                if others:
                    tid = others[0]
                elif rows:
                    raise HTTPException(
                        400,
                        f"'{target_name}' resolves to the same tag. "
                        "Drag one onto the other to merge them.",
                    )
                else:
                    raise HTTPException(400, f"No tag named '{target_name}'")
            try:
                lib.merge_tag(source_id, tid)
            except ValueError as e:
                raise HTTPException(400, str(e))
            row = lib.conn.execute(
                "SELECT name FROM tags WHERE id = ?", (tid,)
            ).fetchone()
        return JSONResponse({"target": row["name"] if row else None})

    @app.post("/tag/delete")
    def tag_delete(tag_id: int = Form(...)):
        with open_lib() as lib:
            lib.delete_tag(tag_id)
        return JSONResponse({"ok": True})

    # -- parse & write ---------------------------------------------------

    @app.post("/preview")
    def preview(
        people: str = Form(""),
        location: str = Form(""),
        context: str = Form(""),
        phototype: str = Form(""),
    ):
        """Parse answers into entity items (no DB writes).

        Each item is ``known`` (an existing tag -> full chain returned for silent
        auto-apply) or ``unknown`` (needs a one-time "what is this?" answer, with
        a fuzzy "did you mean?" suggestion when a near spelling match exists).
        """
        with open_lib() as lib:
            known_names = lib.known_names()
            known_found: list[str] = []
            unknown_found: list[tuple[str, str]] = []
            seen: set[str] = set()
            for box, answer in (
                ("people", people),
                ("location", location),
                ("context", context),
                ("phototype", phototype),
            ):
                category = _BOX_CATEGORY[box]
                for name in llm.parse_answer(answer, category, known_names):
                    disp = lib.canonical_name(name)
                    key = (disp or name).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    if disp:
                        known_found.append(disp)
                    else:
                        unknown_found.append((name, category))

            items: list[dict] = []
            for name in lib.collapse_descendants(known_found):
                items.append({"name": name, "status": "known", "chain": lib.resolve_chain(name)})
            for name, category in unknown_found:
                items.append({
                    "name": name, "status": "unknown",
                    "suggested": category, "did_you_mean": lib.suggest_match(name),
                })
        return JSONResponse({"items": items, "llm": llm.available()})

    @app.post("/learn")
    def learn(name: str = Form(...), chain: str = Form("")):
        """Teach what an entity is (path like 'Pets > Dog') by creating the
        nested tags in the DB. Returns the entity AND every node in its full
        path so the UI can auto-resolve still-pending "what is this?" cards."""
        with open_lib() as lib:
            leaf = lib.learn(name, chain)
            resolved = [
                {"name": node, "chain": lib.resolve_chain(node)}
                for node in lib.full_path(leaf)
            ]
            out = {"name": leaf, "chain": lib.resolve_chain(leaf), "resolved": resolved}
        return JSONResponse(out)

    @app.post("/alias")
    def alias(name: str = Form(...), target: str = Form(...)):
        """Record a typed spelling (``name``) as an alias of existing tag
        ``target``, so it's recognized instantly next time."""
        with open_lib() as lib:
            disp = lib.canonical_name(target)
            if disp is None:
                raise HTTPException(404, "Unknown target entity")
            lib.add_alias_by_name(target, name)
            out = {"name": disp, "chain": lib.resolve_chain(disp)}
        return JSONResponse(out)

    @app.post("/commit")
    def commit(
        current_id: int = Form(...),
        entry_ids: str = Form("[]"),
        entities_json: str = Form("[]"),
    ):
        """Write confirmed entities (each {name, chain}) into one OR MANY photos.

        ``entry_ids`` is a JSON list of the selected photo ids (the filmstrip
        selection, including the current photo). The same tags are applied to
        every selected photo. ``current_id`` is the photo on screen, used to
        decide which photo to advance to next.
        """
        try:
            payload = json.loads(entities_json)
            targets = [int(i) for i in json.loads(entry_ids)]
        except (ValueError, TypeError):
            raise HTTPException(400, "Bad payload")
        if not targets:
            targets = [current_id]
        by_name = {
            (it.get("name") or "").strip(): it
            for it in payload if (it.get("name") or "").strip()
        }
        added: list[str] = []
        with open_lib() as lib:
            keep = {n.lower() for n in lib.collapse_descendants(list(by_name))}
            for tid in targets:
                if lib.get_entry(tid) is None:
                    continue
                for name, item in by_name.items():
                    if name.lower() not in keep:
                        continue
                    chain = [c for c in item.get("chain", []) if c and c.strip()]
                    if lib.apply_entity(tid, name, chain):
                        added.append(name)
            all_ids = [e.id for e in lib.entries()]

        # Advance to the next photo in the feed that wasn't just tagged.
        selected = set(targets)
        next_id = None
        if current_id in all_ids:
            start = all_ids.index(current_id) + 1
            next_id = next((i for i in all_ids[start:] if i not in selected), None)
        return JSONResponse({
            "added": added, "photos": len(targets), "next_id": next_id,
        })

    @app.get("/health")
    def health():
        with open_lib() as lib:
            return {"entries": lib.entry_count(), "library": str(lib.db_path)}

    return app

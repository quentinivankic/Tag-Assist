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
_BOX_CATEGORY = {"people": "People", "location": "Location", "context": "Context"}


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Tag-Assist")
    faces = FaceEngine(config.library)

    def open_lib() -> TagStudioLibrary:
        return TagStudioLibrary(config.library).connect()

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
        return _TEMPLATES.TemplateResponse(
            request, "tags.html", {"tree": tree, "count": count}
        )

    # -- parse & write ---------------------------------------------------

    @app.post("/preview")
    def preview(people: str = Form(""), location: str = Form(""), context: str = Form("")):
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
            for box, answer in (("people", people), ("location", location), ("context", context)):
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

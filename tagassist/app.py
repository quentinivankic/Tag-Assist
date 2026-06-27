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

        if city and lib.is_known(city):
            for child in lib.children(city):        # Moms House, My Apartment...
                if child.lower() not in seen:
                    seen.add(child.lower())
                    options.append({
                        "name": child, "status": "known", "chain": lib.resolve_chain(child),
                    })
            add_known(city)
        elif city:
            path = " > ".join(p for p in ("Location", country, state) if p)
            options.append({"name": city, "status": "unknown", "suggested_path": path})
            seen.add(city.lower())

        if state and state.lower() not in seen:
            if lib.is_known(state):
                add_known(state)
            else:
                path = " > ".join(p for p in ("Location", country) if p)
                options.append({"name": state, "status": "unknown", "suggested_path": path})
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
            all_ids = [e.id for e in lib.entries()]
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
    def commit(entry_id: int = Form(...), entities_json: str = Form("[]")):
        """Write confirmed entities (each {name, chain}) into TagStudio, nested."""
        try:
            payload = json.loads(entities_json)
        except ValueError:
            raise HTTPException(400, "Bad entities payload")
        by_name = {
            (it.get("name") or "").strip(): it
            for it in payload if (it.get("name") or "").strip()
        }
        added: list[str] = []
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None:
                raise HTTPException(404, "Entry not found")
            keep = {n.lower() for n in lib.collapse_descendants(list(by_name))}
            for name, item in by_name.items():
                if name.lower() not in keep:
                    continue
                chain = [c for c in item.get("chain", []) if c and c.strip()]
                if lib.apply_entity(entry_id, name, chain):
                    added.append(name)
            all_ids = [e.id for e in lib.entries()]

        idx = all_ids.index(entry_id) if entry_id in all_ids else -1
        next_id = all_ids[idx + 1] if 0 <= idx < len(all_ids) - 1 else None
        return JSONResponse({"added": added, "next_id": next_id})

    @app.get("/health")
    def health():
        with open_lib() as lib:
            return {"entries": lib.entry_count(), "library": str(lib.db_path)}

    return app

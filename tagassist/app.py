"""FastAPI web app: the interview UI.

Flow:
1.  ``GET /``            - redirect to the first untagged (or first) entry.
2.  ``GET /photo/{id}``  - show the photo, pre-filled hints, the 3 questions.
3.  ``POST /preview``    - parse plain-English answers into tag suggestions
                           (LLM if available, else rule-based) without writing.
4.  ``POST /commit``     - write confirmed tags into the TagStudio library.
5.  ``GET /image/{id}``  - serve the photo bytes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import json

from . import exif, interview, llm
from .config import Config
from .entities import EntityStore
from .faces import FaceEngine
from .tagstudio import TagStudioLibrary

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Which top-level category each interview box suggests for *new* entities.
_BOX_CATEGORY = {"people": "People", "location": "Location", "context": "Context"}


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Tag-Assist")
    entities = EntityStore(config.library)
    faces = FaceEngine(config.library, entities)

    def open_lib() -> TagStudioLibrary:
        return TagStudioLibrary(config.library).connect()

    # -- navigation ------------------------------------------------------

    def _next_untagged_id(lib: TagStudioLibrary) -> int | None:
        for entry in lib.entries():
            # "Untagged" = has no People/Location/Context tag yet.
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
            people_hint = faces.known_people()
            face_suggestions = [m.name for m in faces.suggest(entry.abs_path)] if entry.abs_path.exists() else []

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
                "people_hint": people_hint,
                "face_suggestions": face_suggestions,
                "known_nodes": entities.names(),
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
            return FileResponse(entry.abs_path)

    # -- parse & write ---------------------------------------------------

    @app.post("/preview")
    def preview(
        people: str = Form(""),
        location: str = Form(""),
        context: str = Form(""),
    ):
        """Parse answers into entity items (no DB writes).

        Each item is either ``known`` (already learned -> its full nested chain
        is returned for silent auto-apply) or ``unknown`` (needs a one-time
        "what is this?" answer, pre-suggesting the box's category as the top).
        """
        known_names = entities.names()
        known_found: list[str] = []          # display names of recognized entities
        unknown_found: list[tuple[str, str]] = []  # (name, suggested category)
        seen: set[str] = set()
        for box, answer in (("people", people), ("location", location), ("context", context)):
            category = _BOX_CATEGORY[box]
            for name in llm.parse_answer(answer, category, known_names):
                ent = entities.lookup(name)
                key = (ent.display if ent else name).lower()
                if key in seen:
                    continue
                seen.add(key)
                if ent:
                    known_found.append(ent.display)
                else:
                    unknown_found.append((name, category))

        # Collapse overlaps: keep only the deepest mentioned entity in a chain.
        items: list[dict] = []
        for name in entities.collapse_descendants(known_found):
            items.append({
                "name": name,
                "status": "known",
                "chain": entities.resolve_chain(name),
            })
        for name, category in unknown_found:
            items.append({
                "name": name,
                "status": "unknown",
                "suggested": category,
                "did_you_mean": entities.suggest_match(name),
            })
        return JSONResponse({"items": items, "llm": llm.available()})

    @app.post("/learn")
    def learn(name: str = Form(...), chain: str = Form("")):
        """Teach the app what an entity is (path like 'Pets > Dog'). Remembered.

        Returns the taught entity AND every node in its full path (each with its
        own resolved chain), so the UI can auto-resolve any still-pending
        "what is this?" cards that just became known (e.g. teaching a deep place
        path makes the leftover 'Phoenix' card disappear).
        """
        ent = entities.learn(name, chain)
        resolved = [
            {"name": node, "chain": entities.resolve_chain(node)}
            for node in entities.full_path(ent.display)
        ]
        return JSONResponse({
            "name": ent.display,
            "chain": entities.resolve_chain(ent.display),
            "resolved": resolved,
        })

    @app.post("/alias")
    def alias(name: str = Form(...), target: str = Form(...)):
        """Record that a typed spelling (``name``) is the same as an existing
        entity (``target``), so that spelling is recognized instantly next time.
        Returns the target's canonical name + resolved chain to apply now.
        """
        ent = entities.lookup(target)
        if ent is None:
            raise HTTPException(404, "Unknown target entity")
        entities.add_alias(target, name)
        return JSONResponse({
            "name": ent.display,
            "chain": entities.resolve_chain(ent.display),
        })

    @app.post("/commit")
    def commit(entry_id: int = Form(...), entities_json: str = Form("[]")):
        """Write confirmed entities (each {name, chain}) into TagStudio, nested."""
        try:
            payload = json.loads(entities_json)
        except ValueError:
            raise HTTPException(400, "Bad entities payload")
        # Defensively collapse ancestors the client may have sent.
        by_name = {(it.get("name") or "").strip(): it for it in payload if (it.get("name") or "").strip()}
        keep = {n.lower() for n in entities.collapse_descendants(list(by_name))}
        added: list[str] = []
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None:
                raise HTTPException(404, "Entry not found")
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

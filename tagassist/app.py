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

from . import exif, interview, llm
from .config import Config
from .faces import FaceEngine
from .tagstudio import TagStudioLibrary

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Tag-Assist")
    faces = FaceEngine(config.library)

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
        """Parse answers into suggested tags (no DB writes)."""
        raw = {"People": people, "Location": location, "Context": context}
        suggestions: dict[str, list[str]] = {}
        for category, answer in raw.items():
            tags = llm.parse_answer(answer, category)
            if tags:
                suggestions[category] = tags
        return JSONResponse({"suggestions": suggestions, "llm": llm.available()})

    @app.post("/commit")
    def commit(
        entry_id: int = Form(...),
        people: str = Form(""),
        location: str = Form(""),
        context: str = Form(""),
    ):
        """Write confirmed, comma-separated tags into TagStudio."""
        tags_by_category = {
            "People": [t.strip() for t in people.split(",") if t.strip()],
            "Location": [t.strip() for t in location.split(",") if t.strip()],
            "Context": [t.strip() for t in context.split(",") if t.strip()],
        }
        tags_by_category = {k: v for k, v in tags_by_category.items() if v}
        with open_lib() as lib:
            entry = lib.get_entry(entry_id)
            if entry is None:
                raise HTTPException(404, "Entry not found")
            added = lib.apply_tags(entry_id, tags_by_category)
            all_ids = [e.id for e in lib.entries()]
        # Remember people for quick reuse next time.
        for person in tags_by_category.get("People", []):
            faces.remember_person(person)

        idx = all_ids.index(entry_id) if entry_id in all_ids else -1
        next_id = all_ids[idx + 1] if 0 <= idx < len(all_ids) - 1 else None
        return JSONResponse({"added": added, "next_id": next_id})

    @app.get("/health")
    def health():
        with open_lib() as lib:
            return {"entries": lib.entry_count(), "library": str(lib.db_path)}

    return app

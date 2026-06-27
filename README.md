# Tag-Assist

A self-hosted **AI tagging interviewer for [TagStudio](https://github.com/TagStudioDev/TagStudio)**.

Tag-Assist shows you a photo, asks you in plain English *who* is in it, *where* it
was taken, and *what's happening* — then turns your answers into structured tags
and writes them **directly into your TagStudio library**. Over time it learns
people and places so it can pre-fill them for you.

This is the "conversational tagging layer" that doesn't exist as a finished tool
yet. It works because TagStudio stores its whole library as a plain SQLite file
(`.TagStudio/ts_library.sqlite`), so we can read entries and write tags without a
plugin, fork, or API.

## What it does today (v1)

- 📷 **Interview loop** — walks your library photo-by-photo, asks who / where /
  context, accepts free-text answers.
- 🧠 **Plain-English → tags** — parses your answers into clean tag suggestions you
  confirm or edit before anything is written. Optional local LLM (Ollama) for
  smarter normalization; falls back to a rule-based parser with no GPU needed.
- 🏷️ **Writes into TagStudio** — creates tags under tidy categories (People /
  Location / Context) and links them to the photo, in TagStudio's own database.
- 📍 **EXIF assist** — reads GPS + capture date from the photo to pre-fill the
  date tag and hint at the location.
- 🧩 **Learns over time** — remembers the people/places you've used so they're
  one click next time. (Face-recognition auto-tagging is a clean optional
  module, off by default — see Roadmap.)
- 🌳 **Composable hierarchy** — teach a place's parent *once* (`Phoenix` =
  `Location > USA > Arizona`); after that, anything you nest under it
  (`Moms House` = `Phoenix`) inherits the whole ancestry automatically. Mention
  both a place and something inside it and Tag-Assist collapses to the deepest,
  since TagStudio already makes the parents searchable. New names auto-complete
  from what you've already taught.

## Design principles

- **TagStudio is the source of truth.** We never invent a parallel database for
  your tags; everything lands in `ts_library.sqlite`.
- **Local-first, no cloud.** Nothing leaves your machine. The LLM and
  face-recognition pieces are optional and also run locally.
- **Resilient to TagStudio versions.** Writes introspect the live DB schema
  (`PRAGMA table_info`) and fill required columns, instead of hard-coding a
  schema that drifts between releases.
- **Human-in-the-loop.** Suggestions are always confirmed before they're written.

## Quick start

```bash
pip install -r requirements.txt

# Generate a sample TagStudio library + photos to try it on:
python scripts/make_sample_library.py /tmp/sample-lib

# Point Tag-Assist at a library and run the web UI:
export TAGASSIST_LIBRARY=/tmp/sample-lib
python -m tagassist
# open http://localhost:8765
```

To use your **own** library, set `TAGASSIST_LIBRARY` to the folder that contains
the `.TagStudio` directory. **Close TagStudio first** so writes don't conflict,
and back up `ts_library.sqlite` until you trust it.

## Optional: local LLM (Ollama)

```bash
# Install Ollama and pull a small model, then:
export TAGASSIST_OLLAMA_MODEL=llama3.2
```
If unset or unreachable, Tag-Assist uses the built-in rule-based parser.

## Roadmap

- **Face recognition** (`insightface`/`face_recognition`): cluster faces across
  the library, learn an identity once, auto-suggest it everywhere. Module
  interface is scaffolded in `tagassist/faces.py`.
- **Offline reverse-geocoding** of EXIF GPS → place names.
- **Vision pre-tagging** (Qwen2.5-VL via Ollama) to propose context tags before
  you even answer.

See `tests/` for the behavior that's covered.

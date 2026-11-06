# Architecture

This document explains how QB to Anki is put together, for anyone who wants to understand or change the code. For using the app,
see the [user guide](USER_GUIDE.md).

## Design principles

1. **Nothing is silently trusted.** Every doubt becomes a flag on the card. A *hard* flag holds the card in Review until a person
   approves it; a *soft* flag is only a note. See `qb2anki/flags.py`.
2. **The source wins.** The answer key, printed answers and the book's own explanations always take priority. AI output only fills
   gaps or corrects reading errors, is always labelled, and the first reading is kept so it can be restored.
3. **Never pay twice.** Every Gemini answer is cached on disk (`work/cache`), keyed by model, prompt text, request settings and image
   bytes. Rebuilding, or deleting the database and reading again, reuses those answers.
4. **The database is the source of truth.** The Anki file is generated from it and can be regenerated at any time.
5. **Local only.** The app is a small standard-library web server bound to `127.0.0.1`.

## The reading pipeline (`pipeline.py`)

For each photo in `inbox/`:

| Step | Requests | What happens |
|---|---|---|
| Detect | 1 (auto type only) | `DETECT` prompt: is it a book spread, a single page or a screenshot, and how far must it turn? |
| Layout | 1 (spreads only) | `LAYOUT` prompt returns a box per printed page; `imaging.validate_layout` rejects implausible boxes. If the boxes say the text is not upright, the rotation is detected and the layout retried once. |
| Read | 1 per page | One request per page returns questions, answer-key rows and the exam title (`page_prompt` for book spreads, `general_page_prompt` for everything else). |
| Assemble | 0 | `assemble.assemble_page` normalises labels and digits, joins the answer key, validates structure and raises flags. |
| Proofread | 1 per page | `proofread.py` sends the page picture and the transcription; accepted corrections must look like corrections (similarity guards, no deleted text, no new formula errors). |
| Math check | 1 per 6 math explanations | `mathcheck.py`: local lint, AI formatting repair and consistency check, and a focused vision re-read for anything still broken. |
| Cross-check | 1 per 8 questions | `factcheck.py`, text only: text sanity and repair, independent answer, outdated facts, explanation check, missing answers and explanations. The disagreement policy (`checks.fact_policy`) decides between holding the card, applying sure corrections, or a note. |
| AI fill | 1 per 8 cards with gaps | `fill.py`, only when the cross-check is off: missing answers and explanations. |
| Store | 0 | `db.ingest_question` merges exact duplicates (order-insensitive content hash) and records every appearance. |

Optional cross-check (`checks.cross_check`): each page is read a second time by a different model and differences are flagged.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Defaults → `config.toml` → `settings.json` (written by the app), plus `.env`. Stores paths relative to the app folder. |
| `gemini_client.py` | JSON-schema requests, disk cache, retries with backoff, per-model pacing, fallback models, usage counting, optional key pool. |
| `keys.py` | Optional key rotation (`gemini.key_mode = "rotate"`): least-used key per model counting in-flight requests, daily caps, retiring invalid keys, halting after blocked keys. |
| `schemas.py` | Response schemas sent to Gemini and tolerant pydantic models for what comes back (nulls, numbers, Bengali digits). |
| `prompts.py` | All prompts. The prompt text is part of the cache key. |
| `imaging.py` | Loading (EXIF, transparency), rotation, page-box validation, crops. |
| `assemble.py`, `flags.py` | Page assembly, validation and flags. |
| `proofread.py`, `mathcheck.py`, `fill.py` | The AI quality steps described above. |
| `db.py` | SQLite (WAL mode), migrations, duplicates, exams, usage and key state. |
| `export_anki.py` | Tab-separated Anki import file with GUIDs, decks, tags and the embedded card design. |
| `review.py` | Data for the Review screen. |
| `maintenance.py` | Backups (manual and automatic), restore, resets, debug bundle. |
| `ui.py`, `web/` | The app: HTTP server, background jobs with a live log, and the single-page screen. |
| `__main__.py` | Command line; without arguments it opens the app. |

## Data model (`data/qb.sqlite`)

| Table | Contents |
|---|---|
| `photos` | One row per image: hash, path, detected type, rotation, reader, status and error. |
| `pages` | Printed pages of a photo, with page number, exam and the raw answer key. |
| `exams` | Exam titles found on pages or given as folder names, with a short display label. |
| `questions` | Cards: text, options (with labels as printed), answer and its source (`key`, `printed`, `ai`, `ai_corrected`, `user`), explanations, flags, first readings (`proof_json`), the AI cross-check verdict (`fact_json`), status. |
| `appearances` | Every place a question was seen, so duplicates remember all their exams. |
| `usage`, `key_usage`, `key_state` | Requests per model and day, and the optional key pool's state. |

New columns are added by `DB._ensure_columns`, so older databases keep working.

## Card statuses

`ok` (exportable) · `review` (has a hard flag) · `approved` (checked by a person, exportable) · `rejected` (never exported).
A card is exported once; exporting again with "include cards exported before" refreshes cards in Anki through their stable GUIDs.

## Testing

`tests/` contains unit, pipeline, app-server and export tests. Gemini is replaced by `tests/helpers.FakeClient`, so the suite needs
no key or internet. When the `anki` package is installed, generated files are imported with Anki's own engine.

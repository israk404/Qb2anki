# Changelog

## 1.3.0 — 2026-11-06

### Added
- **Tap to answer** in Anki (on by default, Settings → Anki): tap an option, it turns green (right) or red (wrong), and the card
  turns over to the explanation and Anki's Again / Hard / Good / Easy buttons. Turns over by itself on Anki desktop and AnkiDroid;
  on other apps the colours show at once. Works inside the Basic note type, so nothing has to be set up in Anki and re-importing
  with "Update" keeps study progress.

### Fixed
- No more "ConnectionAbortedError [WinError 10053]" messages in the app window when the browser reloads or closes a page
  while the app is answering. They were harmless; now they are ignored quietly.

## 1.2.0 — 2026-11-06

### Added
- **Full AI cross-check of every question** (text only, 1 request per 8 questions): checks question and options, repairs damaged
  text by matching it to the known MCQ, solves the answer independently, flags outdated facts with the current fact, checks the
  explanation and writes missing ones. Replaces the separate AI-fill step when on.
- **Disagreement policy**: hold for review (default), apply the AI's high-confidence corrections (labelled, original kept), or note only.
- Review: "AI disagrees with the source" filter, the AI's verdict on each card, and buttons to use the AI's or the source's answer
  and the AI explanation.
- Cards show "Answer corrected by AI" and "Update (AI)" lines; tags `Answer::AI_corrected` and `Check::outdated`.
- **Cross-check stored cards** and **Re-check all cards** on the Database page; `check` on the command line.
- **Which key to use**: switch between one key (e.g. a paid key) and rotation through the key list. "Test every key" works in both modes.

### Fixed
- Key rotation now counts requests that are still running, so parallel requests spread evenly over the keys.

## 1.1.0 — 2026-11-06

### Added
- **Automatic photo type**: the AI detects whether an image is a book spread, a single page or a screenshot, and its rotation
  (1 small request per photo). This is now the default type.
- **AI proofreading** of every page against its picture: misread letters and digits, broken or misplaced formulas, ditto marks,
  options attached to the wrong label. The first reading is kept and can be restored in Review.
- **Proofread stored cards** (Database page) for cards read before.
- **Automatic backups** before every task that changes cards; the newest 10 are kept (configurable).
- **Start reading right after adding** photos, and a per-run "detect automatically for all" option to retry failed photos.
- Free tidy-up of LaTeX commands written outside math markers, applied when reading and when exporting.
- Book photos whose usual rotation does not fit are re-turned automatically.

## 1.0.0 — 2026-11-06

First public release.

- Local app with Home, Read photos, Review, Export, Settings, Database and Help pages.
- Reading of book spreads and screenshots in one request per page, with answer keys, printed answers, A/B/C/D labels, figures
  and exam titles.
- Math check and repair, AI answers and explanations for gaps, optional cross-check with a second model.
- Duplicate merging across exams, study-friendly Anki card design, stable GUIDs for updates.
- Saved AI answers, fallback models, quota-aware stopping, per-model usage counts, optional key rotation with safety stops.
- Backups, restore, rebuild, resets and a debug bundle.

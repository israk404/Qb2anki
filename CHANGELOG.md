# Changelog

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

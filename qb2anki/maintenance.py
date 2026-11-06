"""Housekeeping: database backups, resets and the debug bundle."""
from __future__ import annotations

import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path


def backup_db(cfg, db, prefix: str = "qb") -> Path:
    """Copy the database into data\\backups. A name is never reused, so a backup can never overwrite another one."""
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest, n = cfg.backups_dir / f"{prefix}_{stamp}.sqlite", 1
    while dest.exists():
        n += 1
        dest = cfg.backups_dir / f"{prefix}_{stamp}_{n}.sqlite"
    out = sqlite3.connect(str(dest))
    try:
        db.conn.backup(out)          # safe while the app is running (the database is in WAL mode)
    finally:
        out.close()
    return dest


def auto_backup(cfg, db) -> Path | None:
    """The automatic backup before a task (Settings: backup.auto). Keeps the newest `backup.keep` automatic backups;
    backups you made yourself are never deleted."""
    if not cfg.get("backup", "auto", True):
        return None
    if db.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0:
        return None                                   # nothing worth backing up yet
    dest = backup_db(cfg, db, "auto")
    keep = max(1, int(cfg.get("backup", "keep", 10) or 10))
    autos = sorted(cfg.backups_dir.glob("auto_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in autos[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def list_backups(cfg) -> list[dict]:
    if not cfg.backups_dir.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
            for p in sorted(cfg.backups_dir.glob("*.sqlite"), reverse=True)]


def restore_db(cfg, db, name: str) -> None:
    src_path = cfg.backups_dir / Path(name).name
    if not src_path.is_file():
        raise FileNotFoundError(name)
    backup_db(cfg, db, "before_restore")      # always keep what is there now (under its own name)
    src = sqlite3.connect(str(src_path))
    try:
        src.backup(db.conn)
    finally:
        src.close()
    db._ensure_columns("questions", {"answer_source": "TEXT", "explanation_original": "TEXT", "proof_json": "TEXT",
                                     "fact_json": "TEXT"})
    db._ensure_columns("photos", {"profile": "TEXT", "exam_hint": "TEXT", "reader": "TEXT", "kind": "TEXT"})
    db.commit()


def reset(cfg, db, mode: str) -> str:
    """mode: 'exported' (cards can be exported again) | 'records' (forget all cards, keep saved AI answers)
    | 'everything' (also delete crops and saved AI answers: photos must be sent to the AI again)."""
    if mode == "exported":
        db.conn.execute("UPDATE questions SET exported_at=NULL")
        db.commit()
        return "Every card can be exported again."
    if mode in ("records", "everything"):
        backup_db(cfg, db, "before_reset")        # a safety copy, in data\\backups
        for t in ("appearances", "questions", "pages", "photos", "exams"):
            db.conn.execute(f"DELETE FROM {t}")
        db.commit()
        if mode == "everything" and cfg.work.exists():
            shutil.rmtree(cfg.work, ignore_errors=True)
            cfg.ensure_dirs()
            return "All cards, crops and saved AI answers were deleted (a database backup was kept)."
        return "All cards were forgotten (a database backup was kept). Saved AI answers are still there, so reading the photos again costs nothing."
    raise ValueError(mode)


def make_bundle(cfg, with_spreads: bool = False, no_images: bool = False) -> Path:
    """A zip with the database, log, settings, crops and layout pictures: what is needed to find a problem. Never keys."""
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.out_dir / f"debug_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (cfg.db_path, cfg.log_path, cfg.root / "config.toml", cfg.settings_path):
            if p.exists():
                z.write(p, p.relative_to(cfg.root))
        if cfg.work.exists():
            for p in cfg.work.rglob("*"):
                if not p.is_file() or (p.name == "spread.jpg" and not with_spreads):
                    continue
                if no_images and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    continue
                z.write(p, p.relative_to(cfg.root))
    return out

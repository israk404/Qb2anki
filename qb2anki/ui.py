"""The app: a small web server that runs only on this computer (http://127.0.0.1:8765) and controls everything.

Long tasks (reading photos, rebuilding, key tests, ...) run in the background as one 'job' at a time, with a live log.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import export_anki, factcheck, fill, maintenance, models_scan, proofread
from .config import load_config, reset_settings, save_settings
from .db import DB, quota_day, quota_reset_hint
from .errors import GeminiError
from .gemini_client import GeminiClient
from .keys import KeyPool, parse_keys
from .pipeline import IMAGE_EXT, Pipeline, PipelineError, sha256_file
from .review import Review

WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 60 * 1024 * 1024


# --------------------------------------------------------------------------- background jobs
class Busy(RuntimeError):
    pass


class Job:
    def __init__(self, name: str):
        self.id, self.name = uuid.uuid4().hex[:8], name
        self.status = "running"            # running | done | stopped | error
        self.lines: list[str] = []
        self.started, self.ended = time.time(), None
        self.result: dict | None = None
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def say(self, *parts) -> None:
        with self._lock:
            self.lines.append(" ".join(str(p) for p in parts))

    def view(self, since: int = 0) -> dict:
        with self._lock:
            return {"id": self.id, "name": self.name, "status": self.status, "lines": self.lines[since:], "next": len(self.lines),
                    "seconds": round((self.ended or time.time()) - self.started), "result": self.result}


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(level=logging.WARNING)
        self.job = job

    def emit(self, record):
        self.job.say("   · " + record.getMessage()[:220])


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.job: Job | None = None

    def running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start(self, name: str, target) -> Job:
        with self._lock:
            if self.running():
                raise Busy(f"'{self.job.name}' is still running. Wait for it to finish or press Stop.")
            job = Job(name)
            self.job = job

        def wrapper():
            handler = _JobLogHandler(job)
            logging.getLogger("qb2anki").addHandler(handler)
            try:
                job.result = target(job)
                job.status = "stopped" if job.stop.is_set() else "done"
            except GeminiError as e:
                job.say(f"ERROR: {e}")
                job.status = "error"
            except Exception as e:  # noqa: BLE001
                logging.getLogger("qb2anki").exception("job failed")
                job.say(f"ERROR: {type(e).__name__}: {e}")
                job.status = "error"
            finally:
                logging.getLogger("qb2anki").removeHandler(handler)
                job.ended = time.time()
        threading.Thread(target=wrapper, daemon=True).start()
        return job


# --------------------------------------------------------------------------- the application
_PATCH_RULES = {
    "gemini": {"layout_model": str, "fill_model": str, "extract_models": list, "fallback_models": list, "requests_per_minute": int,
               "concurrency": int, "key_daily_cap": int, "halt_after_blocked": int, "thinking_level": str, "key_mode": str},
    "checks": {"proofread": bool, "fact_check": bool, "fact_policy": str, "cross_check": bool, "math_check": bool, "ai_fill": bool, "block_export_on_soft_flags": bool,
               "near_duplicate_ratio": float},
    "backup": {"auto": bool, "keep": int},
    "anki": {"deck_root": str, "notetype": str, "source_line": str, "page_tag": bool, "tap_to_answer": bool},
}


def clean_patch(patch: dict) -> dict:
    """Keep only settings the Settings page may change, with the right types. Raises ValueError with a readable message."""
    out: dict = {}
    for section, rules in _PATCH_RULES.items():
        for key, typ in rules.items():
            if key not in patch.get(section, {}):
                continue
            v = patch[section][key]
            try:
                if typ is bool:
                    v = bool(v)
                elif typ is int:
                    v = int(v)
                    if v < 0:
                        raise ValueError
                elif typ is float:
                    v = float(v)
                elif typ is list:
                    v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).replace(",", "\n").splitlines()) if str(x).strip()]
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                raise ValueError(f"'{key}' has an invalid value") from None
            if key == "key_mode" and v not in ("single", "rotate"):
                raise ValueError("key_mode must be single or rotate")
            if key == "fact_policy" and v not in factcheck.POLICIES:
                raise ValueError("fact_policy must be review, auto or note")
            if key == "source_line" and v not in ("exam", "full", "none"):
                raise ValueError("source_line must be exam, full or none")
            if key == "extract_models" and not v:
                raise ValueError("extract_models needs at least one model")
            if key == "thinking_level" and v == "":
                continue
            if key == "keep" and v < 1:
                raise ValueError("keep at least one automatic backup")
            out.setdefault(section, {})[key] = v
    if "subjects" in patch:
        subj = {str(k).strip(): str(v).strip() for k, v in dict(patch["subjects"]).items() if str(k).strip()}
        if not subj:
            raise ValueError("at least one subject is needed")
        out["subjects"] = subj
    if "profile_rotate" in patch:
        prof = {}
        for name, rot in dict(patch["profile_rotate"]).items():
            if name == "auto":
                continue
            rot = str(rot).strip().lower()
            if rot != "auto" and rot not in ("0", "90", "180", "270"):
                raise ValueError("rotation must be 0, 90, 180, 270 or auto")
            prof[name] = {"rotate": rot if rot == "auto" else int(rot)}
        out["profiles"] = prof
    return out


class App:
    def __init__(self, root, client_factory=None):
        self.root = Path(root)
        self.cfg = load_config(self.root)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.lock = threading.RLock()
        self.review = Review(self.cfg, self.db)
        self.jobs = JobManager()
        self.client_factory = client_factory or (lambda cfg, db: GeminiClient(cfg, use_cache=True, db=db))
        self.token = secrets.token_urlsafe(18)
        self._sha_cache: dict = {}
        self.httpd = None

    def reload(self) -> None:
        self.cfg = load_config(self.root)
        self.cfg.ensure_dirs()
        self.review.cfg = self.cfg

    def _pipe(self) -> Pipeline:
        return Pipeline(self.cfg, self.db, None)

    # ------------------------------------------------------------------ keys and key status
    def has_api_key(self) -> bool:
        env = os.environ.get(self.cfg.get("gemini", "api_key_env", "GEMINI_API_KEY"))
        keys = self.cfg.keys_path
        return bool(env) or (keys.exists() and bool(parse_keys(keys.read_text(encoding="utf-8", errors="ignore"))))

    def keys_info(self) -> dict:
        path = self.cfg.keys_path
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        env = os.environ.get(self.cfg.get("gemini", "api_key_env", "GEMINI_API_KEY")) or ""
        pool = KeyPool.from_config(self.cfg, self.db)
        return {"text": text, "count": len(parse_keys(text)), "main_hint": ("…" + env[-4:]) if env else "",
                "mode": self.cfg.get("gemini", "key_mode", "single"),
                "rotation": pool.summary() if pool else None}

    def save_keys(self, text: str) -> dict:
        path = self.cfg.keys_path
        if (text or "").strip():
            path.write_text(text.strip() + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
        return {"ok": True, "count": len(parse_keys(text or ""))}

    def save_main_key(self, key: str) -> dict:
        key = (key or "").strip()
        if len(key) < 20 or " " in key:
            return {"error": "That does not look like an API key."}
        env_name = self.cfg.get("gemini", "api_key_env", "GEMINI_API_KEY")
        lines = self.cfg.env_path.read_text(encoding="utf-8").splitlines() if self.cfg.env_path.exists() else []
        lines = [l for l in lines if not l.strip().startswith(env_name + "=")] + [f"{env_name}={key}"]
        self.cfg.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[env_name] = key
        return {"ok": True, "hint": "…" + key[-4:]}

    # ------------------------------------------------------------------ status, inbox, upload
    def inbox(self) -> list[dict]:
        pipe, out = self._pipe(), []
        for f in pipe.list_inbox():
            st = f.stat()
            sig = (str(f), st.st_mtime_ns, st.st_size)
            sha = self._sha_cache.get(sig) or self._sha_cache.setdefault(sig, sha256_file(f))
            row = self.db.photo_by_sha(sha)
            status = "waiting" if not row else {"done": "done", "failed": "failed"}.get(row["status"], "waiting")
            prof, _, hint = pipe.resolve_profile(f)
            if row and row["kind"] and prof == "auto":
                prof = "auto → " + {"spread": "book spread", "page": "single page", "screenshot": "screenshot"}.get(row["kind"], row["kind"])
            out.append({"path": str(f.relative_to(self.cfg.inbox)).replace("\\", "/"), "name": f.name, "profile": prof, "exam": hint or "",
                        "status": status, "error": (row["error"] if row and row["status"] == "failed" else "") or "",
                        "kb": round(st.st_size / 1024)})
        return out

    def status(self) -> dict:
        with self.lock:
            stats = self.db.stats()
            inbox = self.inbox()
            q = lambda sql: self.db.conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "stats": stats, "usage": self.db.usage_for(quota_day()), "reset_hint": quota_reset_hint(),
                "inbox": {"waiting": sum(1 for f in inbox if f["status"] != "done"), "total": len(inbox)},
                "exportable": q("SELECT COUNT(*) FROM questions WHERE status IN ('ok','approved') AND exported_at IS NULL"),
                "counts": self.review.counts(),
                "has_key": self.has_api_key(), "rotation": self.keys_info()["rotation"],
                "profiles": {k: v for k, v in self.cfg.raw["profiles"].items() if isinstance(v, dict)},
                "default_profile": self.cfg.raw["profiles"].get("default", "book"),
                "mode": {"proofread": self.pipe_flag("proofread"), "fact_check": self.pipe_flag("fact_check"),
                         "fact_policy": self.cfg.get("checks", "fact_policy", "review"),
                         "key_mode": self.cfg.get("gemini", "key_mode", "single"), "cross_check": self.pipe_flag("cross_check"),
                         "math_check": self.pipe_flag("math_check"), "ai_fill": self.pipe_flag("ai_fill"),
                         "auto_backup": bool(self.cfg.get("backup", "auto", True))},
                "busy": self.jobs.running(),
            }

    def pipe_flag(self, name: str) -> bool:
        return bool(self.cfg.get("checks", name, False))

    def upload(self, filename: str, profile: str, exam: str, data: bytes) -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in IMAGE_EXT:
            return {"error": f"'{filename}' is not an image I can read ({', '.join(sorted(IMAGE_EXT))})."}
        profiles = [k for k, v in self.cfg.raw["profiles"].items() if isinstance(v, dict)]
        profile = profile if profile in profiles else self.cfg.raw["profiles"].get("default", "book")
        clean = lambda s: re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")  # noqa: E731
        detect = bool(self.cfg.raw["profiles"].get(profile, {}).get("detect"))
        folder = self.cfg.inbox if detect else self.cfg.inbox / clean(profile)
        if exam.strip():
            folder = folder / clean(exam.strip())[:80]
        folder.mkdir(parents=True, exist_ok=True)
        stem, target, n = clean(Path(filename).stem) or "image", None, 0
        while target is None or target.exists():
            target = folder / f"{stem}{'' if n == 0 else f'_{n}'}{ext}"
            n += 1
        target.write_bytes(data)
        return {"ok": True, "path": str(target.relative_to(self.cfg.inbox)).replace("\\", "/")}

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _backup_first(cfg, db, job: Job) -> None:
        try:
            b = maintenance.auto_backup(cfg, db)
            if b:
                job.say(f"Automatic backup saved: {b.name}")
        except Exception as e:  # noqa: BLE001  (a failed backup is reported but does not stop the task)
            job.say(f"Automatic backup failed: {e}")

    def _job_run(self, body: dict):
        limit = int(body.get("limit") or 0) or None
        rotate = str(body.get("rotate") or "").strip().lower() or None
        if rotate and rotate != "auto":
            rotate = int(rotate)
        only = body.get("files") or None
        profile = (body.get("profile") or "").strip() or None
        if profile and not isinstance(self.cfg.raw["profiles"].get(profile), dict):
            raise ValueError(f"unknown photo type '{profile}'")

        def target(job: Job):
            cfg = self.cfg
            db = DB(cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                pipe = Pipeline(cfg, db, client, say=job.say, should_stop=job.stop.is_set)
                files = [cfg.inbox / f for f in only] if only else pipe.pending()
                if not files:
                    job.say("Nothing to read: no new photos in the inbox.")
                    return {"photos": 0}
                self._backup_first(cfg, db, job)
                job.say(f"Reading {min(len(files), limit or len(files))} of {len(files)} waiting photo(s)...")
                rep = pipe.run(files, rotate=rotate, limit=limit, profile=profile)
                job.say("")
                job.say(f"Done: {rep['photos']} photo(s) read, {rep['inserted']} new questions, {rep['duplicate']} duplicates merged, "
                        f"{rep['review']} need your review.")
                for name, msg in rep["failed"]:
                    job.say(f"FAILED {name}: {msg[:300]}")
                if rep.get("quota_stop"):
                    job.say("STOPPED EARLY: " + rep["quota_stop"])
                    if "blocked" in rep["quota_stop"].lower():
                        job.say("Nothing more is requested until you reset the key marks (Settings > Keys).")
                    else:
                        job.say(f"Free quotas reset at midnight Pacific time = {quota_reset_hint()}. Finished work is saved; run again then.")
                if rep.get("stopped"):
                    job.say("Stopped by you. Finished photos are saved.")
                return rep
            finally:
                db.close()
        return self.jobs.start("Reading photos", target)

    def _job_rebuild(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                pipe = Pipeline(cfg, db, client, say=lambda *_: None, should_stop=job.stop.is_set)
                self._backup_first(cfg, db, job)
                rows = db.conn.execute("SELECT * FROM photos WHERE status='done'").fetchall()
                done = skipped = 0
                for row in rows:
                    if job.stop.is_set():
                        break
                    try:
                        rep = pipe.rerun(row["id"], force=bool(body.get("force")))
                        done += 1
                        job.say(f"{row['stem']}: {rep['inserted']} questions, {rep['review']} need review")
                    except PipelineError as e:
                        skipped += 1
                        job.say(f"{row['stem']}: skipped ({e})")
                job.say(f"Rebuilt {done} photo(s), skipped {skipped}. Requests used: {client.api_calls} (0 means everything came from saved answers).")
                return {"rebuilt": done, "skipped": skipped}
            finally:
                db.close()
        return self.jobs.start("Rebuilding from saved answers", target)

    def _job_fill(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                self._backup_first(cfg, db, job)
                r = fill.fill_database(db, client, cfg.get("gemini", "fill_model") or cfg.extract_models[0], say=job.say,
                                       should_stop=job.stop.is_set)
                job.say(f"AI wrote {r['answered']} answer(s) and {r['explained']} explanation(s) for {r['candidates']} card(s) that had gaps.")
                Pipeline(cfg, db, client).flush_usage()
                return r
            finally:
                db.close()
        return self.jobs.start("Filling gaps with AI", target)

    def _job_proofread(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                self._backup_first(cfg, db, job)
                job.say("Proofreading stored cards against their page pictures (1 request per page)...")
                r = proofread.proofread_database(cfg, db, client, cfg.get("gemini", "fill_model") or cfg.extract_models[0],
                                                 say=job.say, should_stop=job.stop.is_set)
                job.say(f"Done: {r['pages']} page(s) checked, {r['changed']} card(s) corrected."
                        + (f" {r['skipped']} page(s) skipped (picture missing)." if r["skipped"] else "")
                        + (" Corrected cards that were exported before can be exported again (Anki: Existing notes = Update)." if r["changed"] else ""))
                Pipeline(cfg, db, client).flush_usage()
                return r
            finally:
                db.close()
        return self.jobs.start("Proofreading stored cards", target)

    def _job_factcheck(self, body: dict):
        recheck = bool(body.get("recheck"))

        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                self._backup_first(cfg, db, job)
                job.say("Cross-checking stored cards: question, options, answer, up-to-dateness, explanation (1 request per 8 cards)...")
                r = factcheck.check_database(cfg, db, client, cfg.get("gemini", "fill_model") or cfg.extract_models[0],
                                             say=job.say, should_stop=job.stop.is_set, recheck=recheck)
                job.say(f"Done: {r['checked']} card(s) checked; {r['held']} wait in Review because the AI disagreed with the source.")
                Pipeline(cfg, db, client).flush_usage()
                return r
            finally:
                db.close()
        return self.jobs.start("Cross-checking stored cards", target)

    def _job_doctor(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                schema = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}, "required": ["ok"]}
                models = list(dict.fromkeys([cfg.get("gemini", "layout_model"), *cfg.extract_models, cfg.get("gemini", "fill_model"),
                                             *cfg.get("gemini", "fallback_models", [])]))
                job.say(f"Python {sys.version.split()[0]}; testing {len(models)} model(s) with one tiny request each...")
                ok = 0
                for m in models:
                    if job.stop.is_set():
                        break
                    try:
                        client.generate_json(model=m, prompt='Reply with {"ok": true}', schema=schema, system=None, label="doctor",
                                             allow_fallback=False)
                        job.say(f"  OK       {m}")
                        ok += 1
                    except GeminiError as e:
                        job.say(f"  FAILED   {m}: {str(e).split('] ', 1)[-1][:160]}")
                job.say(f"{ok} of {len(models)} models work. Fix the failed ones in Settings > Models (or remove them from the fallback list).")
                Pipeline(cfg, db, client).flush_usage()
                return {"ok": ok, "models": len(models)}
            finally:
                db.close()
        return self.jobs.start("Checking setup", target)

    def _job_models_scan(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                client = self.client_factory(cfg, db)
                r = models_scan.scan(client, say=job.say, should_stop=job.stop.is_set)
                Pipeline(cfg, db, client).flush_usage()
                return r
            finally:
                db.close()
        return self.jobs.start("Finding working models", target)

    def _job_keys_check(self, body: dict):
        def target(job: Job):
            cfg, db = self.cfg, DB(self.cfg.db_path)
            try:
                pool = KeyPool.from_config(cfg, db, any_mode=True)
                if pool is None:
                    job.say("No key list found: put two or more keys in Settings > Google AI key first.")
                    return {}
                client = self.client_factory(cfg, db)
                model = cfg.get("gemini", "layout_model")
                job.say(f"Testing {len(pool.keys)} keys with one tiny request each on {model}...")
                counts: dict = {}
                for k in pool.keys:
                    if job.stop.is_set():
                        break
                    status, note = client.check_key(k.secret, model)
                    counts[status] = counts.get(status, 0) + 1
                    if status in ("bad", "blocked"):
                        pool.on_key_rejected(k, status, note, halt=False)
                    job.say(f"  {k.label} …{k.secret[-4:]}: {status}" + (f" ({note})" if note and status != "ok" else ""))
                    time.sleep(0.5)
                pool.flush(db)
                job.say("Result: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: -x[1])))
                return counts
            finally:
                db.close()
        return self.jobs.start("Testing keys", target)

    # ------------------------------------------------------------------ settings
    def get_settings(self) -> dict:
        g, c, a = self.cfg.raw["gemini"], self.cfg.raw["checks"], self.cfg.raw["anki"]
        return {"gemini": {k: g.get(k, "" if t is not list else []) for k, t in _PATCH_RULES["gemini"].items()},
                "checks": {k: c.get(k) for k in _PATCH_RULES["checks"]}, "anki": {k: a.get(k) for k in _PATCH_RULES["anki"]},
                "backup": {"auto": bool(self.cfg.get("backup", "auto", True)), "keep": int(self.cfg.get("backup", "keep", 10))},
                "subjects": self.cfg.subjects,
                "profiles": {k: {"rotate": v.get("rotate"), "layout": v.get("layout"), "reader": v.get("reader")}
                             for k, v in self.cfg.raw["profiles"].items() if isinstance(v, dict) and not v.get("detect")},
                "has_overrides": self.cfg.settings_path.exists()}

    # ------------------------------------------------------------------ misc actions
    def open_folder(self, what: str) -> dict:
        path = {"inbox": self.cfg.inbox, "out": self.cfg.out_dir, "data": self.cfg.data_dir, "work": self.cfg.work,
                "backups": self.cfg.backups_dir, "root": self.root}.get(what)
        if path is None:
            return {"error": "unknown folder"}
        path.mkdir(parents=True, exist_ok=True)
        if os.environ.get("QB_NO_OPEN"):
            return {"ok": True, "path": str(path)}
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:  # noqa: BLE001
            return {"error": f"Could not open the folder: {e}", "path": str(path)}
        return {"ok": True, "path": str(path)}

    def export(self, body: dict) -> dict:
        with self.lock:
            try:
                r = export_anki.export(self.cfg, self.db, include_all=bool(body.get("all")), anki_media=(body.get("anki_media") or "").strip() or None)
            except FileNotFoundError as e:
                return {"error": str(e)}
        return {"count": r["count"], "file": r["path"].name if r["path"] else None, "path": str(r["path"] or ""),
                "skipped": [{"id": i, "why": w} for i, w in r["skipped"]]}

    def export_info(self) -> dict:
        with self.lock:
            q = lambda sql: self.db.conn.execute(sql).fetchone()[0]  # noqa: E731
            files = sorted(self.cfg.out_dir.glob("anki_*.txt"), reverse=True)[:8]
            return {"ready": q("SELECT COUNT(*) FROM questions WHERE status IN ('ok','approved') AND exported_at IS NULL"),
                    "exported": q("SELECT COUNT(*) FROM questions WHERE exported_at IS NOT NULL"),
                    "held": q("SELECT COUNT(*) FROM questions WHERE status='review'"),
                    "files": [f.name for f in files], "media_dirs": export_anki.find_anki_media_dirs(),
                    "deck_root": self.cfg.get("anki", "deck_root"), "notetype": self.cfg.get("anki", "notetype")}


# --------------------------------------------------------------------------- HTTP
def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # -- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                # the browser closed the connection first (page reloaded or closed): nothing to answer, not an error
                self.close_connection = True

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            port = app.httpd.server_address[1] if app.httpd else 0
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _api_ok(self) -> bool:
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-QB-Token") or "", app.token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # -- GET
        def do_GET(self):
            u = urlparse(self.path)
            try:
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                if u.path == "/":
                    page = (WEB / "index.html").read_text(encoding="utf-8").replace("__QB_TOKEN__", app.token)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if u.path in ("/static/app.css", "/static/app.js"):
                    f = WEB / u.path.rsplit("/", 1)[1]
                    return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "text/plain")
                if u.path.startswith("/file/"):
                    target = (app.cfg.work / unquote(u.path[len("/file/"):])).resolve()
                    try:
                        target.relative_to(app.cfg.work.resolve())
                    except ValueError:
                        return self._send(403, b"forbidden", "text/plain")
                    if not target.is_file():
                        return self._send(404, b"missing", "text/plain")
                    return self._send(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                if u.path.startswith("/download/"):
                    base = app.cfg.out_dir.resolve()
                    target = (base / unquote(u.path[len("/download/"):])).resolve()
                    try:
                        target.relative_to(base)
                    except ValueError:
                        return self._send(403, b"forbidden", "text/plain")
                    if not target.is_file():
                        return self._send(404, b"missing", "text/plain")
                    return self._send(200, target.read_bytes(), "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{target.name}"'})
                if not u.path.startswith("/api/"):
                    return self._send(404, b"not found", "text/plain")
                if not self._api_ok():
                    return self._json({"error": "unauthorised"}, 403)
                qs = parse_qs(u.query)
                p = u.path
                if p == "/api/status":
                    return self._json(app.status())
                if p == "/api/inbox":
                    with app.lock:
                        return self._json(app.inbox())
                if p == "/api/job":
                    j = app.jobs.job
                    return self._json(j.view(int(qs.get("since", ["0"])[0])) if j else {"status": "none"})
                if p == "/api/queue":
                    with app.lock:
                        return self._json(app.review.queue(qs.get("f", ["review"])[0]))
                if p.startswith("/api/question/"):
                    with app.lock:
                        d = app.review.detail(int(p.rsplit("/", 1)[1]))
                    return self._json(d) if d else self._json({"error": "not found"}, 404)
                if p.startswith("/api/preview/"):
                    with app.lock:
                        d = export_anki.preview(app.cfg, app.db, int(p.rsplit("/", 1)[1]))
                    return self._json(d) if d else self._json({"error": "this card has no answer yet"}, 404)
                if p == "/api/settings":
                    return self._json(app.get_settings())
                if p == "/api/keys":
                    with app.lock:
                        return self._json(app.keys_info())
                if p == "/api/export/info":
                    return self._json(app.export_info())
                if p == "/api/backups":
                    return self._json(maintenance.list_backups(app.cfg))
                return self._send(404, b"not found", "text/plain")
            except Exception as e:  # noqa: BLE001
                logging.getLogger("qb2anki").exception("GET %s", self.path)
                self._json({"error": str(e)}, 500)

        # -- POST
        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            try:
                if not self._api_ok():
                    return self._json({"error": "unauthorised"}, 403)
                if p == "/api/upload":
                    n = int(self.headers.get("Content-Length", 0))
                    if n > MAX_UPLOAD:
                        return self._json({"error": "That file is too large (limit 60 MB)."}, 413)
                    data = self.rfile.read(n)
                    r = app.upload(unquote(self.headers.get("X-Filename", "image.jpg")), unquote(self.headers.get("X-Profile", "")),
                                   unquote(self.headers.get("X-Exam", "")), data)
                    return self._json(r, 400 if "error" in r else 200)
                body = self._body()
                starters = {"/api/run": app._job_run, "/api/rebuild": app._job_rebuild, "/api/fill": app._job_fill,
                            "/api/doctor": app._job_doctor, "/api/keys/check": app._job_keys_check,
                            "/api/models/scan": app._job_models_scan, "/api/proofread": app._job_proofread,
                            "/api/factcheck": app._job_factcheck}
                if p in starters:
                    try:
                        job = starters[p](body)
                    except Busy as e:
                        return self._json({"error": str(e)}, 409)
                    except ValueError as e:
                        return self._json({"error": f"Invalid option: {e}"}, 400)
                    return self._json({"ok": True, "job": job.id})
                if p == "/api/job/stop":
                    if app.jobs.job and app.jobs.running():
                        app.jobs.job.stop.set()
                        app.jobs.job.say("Stopping after the current photo...")
                    return self._json({"ok": True})
                if p == "/api/settings":
                    try:
                        patch = clean_patch(body)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    save_settings(app.root, patch)
                    app.reload()
                    return self._json({"ok": True})
                if p == "/api/settings/reset":
                    reset_settings(app.root)
                    app.reload()
                    return self._json({"ok": True})
                if p == "/api/keys":
                    with app.lock:
                        return self._json(app.save_keys(body.get("text", "")))
                if p == "/api/apikey":
                    r = app.save_main_key(body.get("key", ""))
                    return self._json(r, 400 if "error" in r else 200)
                if p == "/api/keys/reset":
                    with app.lock:
                        app.db.clear_key_states()
                    return self._json({"ok": True})
                if p == "/api/export":
                    r = app.export(body)
                    return self._json(r, 400 if "error" in r else 200)
                if p == "/api/open":
                    r = app.open_folder(body.get("what", ""))
                    return self._json(r, 400 if "error" in r else 200)
                if p in ("/api/db/backup", "/api/db/restore", "/api/db/reset", "/api/bundle"):
                    if app.jobs.running():
                        return self._json({"error": "A task is running. Wait for it to finish first."}, 409)
                    with app.lock:
                        if p == "/api/db/backup":
                            return self._json({"ok": True, "name": maintenance.backup_db(app.cfg, app.db).name})
                        if p == "/api/db/restore":
                            maintenance.restore_db(app.cfg, app.db, body.get("name", ""))
                            return self._json({"ok": True})
                        if p == "/api/bundle":
                            return self._json({"ok": True, "file": maintenance.make_bundle(app.cfg, no_images=bool(body.get("no_images"))).name})
                        mode = body.get("mode", "")
                        if mode in ("records", "everything") and body.get("confirm") != "DELETE":
                            return self._json({"error": "Type DELETE to confirm."}, 400)
                        return self._json({"ok": True, "message": maintenance.reset(app.cfg, app.db, mode)})
                if p.startswith("/api/question/"):
                    with app.lock:
                        r = app.review.save(int(p.rsplit("/", 1)[1]), body)
                    return self._json(r, 400 if "error" in r else 200)
                if p == "/api/exam/new":
                    with app.lock:
                        r = app.review.name_exam_for(int(body.get("question_id", 0)), body.get("label", ""))
                    return self._json(r, 400 if "error" in r else 200)
                if p.startswith("/api/exam/"):
                    with app.lock:
                        return self._json(app.review.set_exam_label(int(p.rsplit("/", 1)[1]), body.get("label", "")))
                if p == "/api/quit":
                    self._json({"ok": True})
                    threading.Thread(target=app.httpd.shutdown, daemon=True).start()
                    return
                return self._send(404, b"not found", "text/plain")
            except FileNotFoundError as e:
                self._json({"error": f"Not found: {e}"}, 404)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("qb2anki").exception("POST %s", self.path)
                self._json({"error": str(e)}, 500)

    return Handler


def serve(root, open_browser: bool = True, client_factory=None):
    app = App(root, client_factory)
    port = int(app.cfg.get("ui", "port", 8765))
    url = f"http://127.0.0.1:{port}/"
    try:
        app.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        app.db.close()
        print(f"Port {port} is already in use: the app is probably open already, so it is opened in your browser.")
        print("If another program uses this port, change [ui] port in config.toml.")
        if open_browser:
            webbrowser.open(url)
        return False
    print(f"QB to Anki is running at {url}")
    print("Keep this window open while you use the app. Close it (or use Quit in the app) to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        app.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        app.httpd.server_close()
        app.db.close()

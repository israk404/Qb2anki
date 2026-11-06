"""SQLite storage. The database is the source of truth; Anki files are generated from it."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .textnorm import content_hash, norm_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, path TEXT, stem TEXT, rotate INTEGER,
  status TEXT, error TEXT, layout_json TEXT, workdir TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS exams(
  id INTEGER PRIMARY KEY, title TEXT, exam_date TEXT, info_json TEXT, label TEXT,
  UNIQUE(title, exam_date));
CREATE TABLE IF NOT EXISTS pages(
  id INTEGER PRIMARY KEY, photo_id INTEGER, idx INTEGER, printed_page INTEGER, exam_id INTEGER,
  has_header INTEGER, key_json TEXT, flags_json TEXT, UNIQUE(photo_id, idx));
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY, guid TEXT UNIQUE, page_id INTEGER, col_idx INTEGER, number INTEGER, exam_id INTEGER,
  subject TEXT, stem TEXT, norm_stem TEXT, options_json TEXT, correct_label TEXT, key_raw TEXT,
  explanation TEXT, ai_explanation TEXT, figure_path TEXT, crop_path TEXT,
  flags_json TEXT, status TEXT, content_hash TEXT, alt_json TEXT,
  exported_at TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_q_hash ON questions(content_hash);
CREATE INDEX IF NOT EXISTS idx_q_status ON questions(status);
CREATE TABLE IF NOT EXISTS usage(
  day TEXT, model TEXT, calls INTEGER, input_tokens INTEGER, output_tokens INTEGER, PRIMARY KEY(day, model));
CREATE TABLE IF NOT EXISTS key_usage(
  day TEXT, key_id TEXT, model TEXT, calls INTEGER DEFAULT 0, exhausted INTEGER DEFAULT 0, PRIMARY KEY(day, key_id, model));
CREATE TABLE IF NOT EXISTS key_state(key_id TEXT PRIMARY KEY, state TEXT, note TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS appearances(
  id INTEGER PRIMARY KEY, question_id INTEGER, exam_id INTEGER, number INTEGER, printed_page INTEGER, photo_id INTEGER);
"""

_JSON_COLS = {"options_json": "options", "flags_json": "flags", "alt_json": "alt", "proof_json": "proof", "fact_json": "fact"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quota_day() -> str:
    """Google's free-tier daily quotas reset at midnight Pacific time, so 'today' is counted in that time zone."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001  (Windows without the tzdata package)
        return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def quota_reset_hint() -> str:
    """When the free daily quotas reset, in this computer's local time."""
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        reset = (datetime.now(pt) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return reset.astimezone().strftime("%H:%M on %d %b (your computer's time)")
    except Exception:  # noqa: BLE001
        return "about 07:00-08:00 UTC"


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


class DB:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")          # the app reads while a background job writes
        self.conn.executescript(SCHEMA)
        self._ensure_columns("questions", {"answer_source": "TEXT", "explanation_original": "TEXT", "proof_json": "TEXT",
                                             "fact_json": "TEXT"})
        self._ensure_columns("photos", {"profile": "TEXT", "exam_hint": "TEXT", "reader": "TEXT", "kind": "TEXT"})
        self.conn.commit()

    def _ensure_columns(self, table: str, cols: dict) -> None:
        """Add columns that older databases do not have yet."""
        have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def close(self):
        self.conn.close()

    def commit(self):
        self.conn.commit()

    # ------------------------------------------------------------------ photos
    def photo_by_sha(self, sha: str):
        return self.conn.execute("SELECT * FROM photos WHERE sha256=?", (sha,)).fetchone()

    def photo(self, photo_id: int):
        return self.conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()

    def photo_by_stem(self, stem: str):
        return self.conn.execute("SELECT * FROM photos WHERE stem=?", (stem,)).fetchone()

    def add_photo(self, sha: str, path: str, stem: str, rotate: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO photos(sha256,path,stem,rotate,status,created_at) VALUES(?,?,?,?,?,?)",
            (sha, path, stem, rotate, "new", now()))
        self.conn.commit()
        return cur.lastrowid

    def update_photo(self, photo_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE photos SET {cols} WHERE id=?", (*fields.values(), photo_id))
        self.conn.commit()

    def delete_photo_data(self, photo_id: int):
        """Remove pages/questions/appearances produced from one photo (used by `rerun`)."""
        page_ids = [r["id"] for r in self.conn.execute("SELECT id FROM pages WHERE photo_id=?", (photo_id,))]
        for pid in page_ids:
            self.conn.execute("DELETE FROM questions WHERE page_id=?", (pid,))
        self.conn.execute("DELETE FROM appearances WHERE photo_id=?", (photo_id,))
        self.conn.execute("DELETE FROM pages WHERE photo_id=?", (photo_id,))
        self.conn.commit()

    def photo_has_protected_questions(self, photo_id: int) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) c FROM questions q JOIN pages p ON q.page_id=p.id "
            "WHERE p.photo_id=? AND (q.status='approved' OR q.exported_at IS NOT NULL)", (photo_id,)).fetchone()
        return r["c"] > 0

    # ------------------------------------------------------------------ exams
    def get_or_create_exam(self, title: str, exam_date: str | None, info: dict) -> int:
        title = (title or "").strip()
        r = self.conn.execute("SELECT id FROM exams WHERE title=? AND IFNULL(exam_date,'')=?",
                              (title, exam_date or "")).fetchone()
        if r:
            return r["id"]
        cur = self.conn.execute("INSERT INTO exams(title,exam_date,info_json,label) VALUES(?,?,?,?)",
                                (title, exam_date or "", _j(info), None))
        self.conn.commit()
        return cur.lastrowid

    def exams(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM exams ORDER BY id")]

    def exam(self, exam_id: int | None):
        if exam_id is None:
            return None
        r = self.conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
        return dict(r) if r else None

    def set_exam_label(self, exam_id: int, label: str):
        self.conn.execute("UPDATE exams SET label=? WHERE id=?", (label.strip() or None, exam_id))
        self.conn.commit()

    def exam_display(self, exam_id: int | None) -> str:
        e = self.exam(exam_id)
        if not e:
            return "Unknown exam"
        return e["label"] or (e["title"][:50] if e["title"] else "Unknown exam")

    # ------------------------------------------------------------------ pages
    def add_page(self, photo_id: int, idx: int, printed_page, exam_id, has_header: bool, key_json, flags) -> int:
        cur = self.conn.execute(
            "INSERT INTO pages(photo_id,idx,printed_page,exam_id,has_header,key_json,flags_json) VALUES(?,?,?,?,?,?,?)",
            (photo_id, idx, printed_page, exam_id, int(has_header), _j(key_json), _j(flags)))
        self.conn.commit()
        return cur.lastrowid

    def exam_of_printed_page(self, printed_page: int | None):
        if printed_page is None:
            return None
        r = self.conn.execute(
            "SELECT exam_id FROM pages WHERE printed_page=? AND exam_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (printed_page,)).fetchone()
        return r["exam_id"] if r else None

    def _adopt_exam(self, page_id: int, exam_id: int) -> None:
        """Give a page, and its questions that have no exam yet, an exam (and drop the 'exam unknown' note)."""
        self.conn.execute("UPDATE pages SET exam_id=? WHERE id=?", (exam_id, page_id))
        for q in self.conn.execute("SELECT id, flags_json FROM questions WHERE page_id=? AND exam_id IS NULL", (page_id,)).fetchall():
            flags = [f for f in json.loads(q["flags_json"] or "[]") if f.get("code") != "exam_unknown"]
            self.conn.execute("UPDATE questions SET exam_id=?, flags_json=? WHERE id=?", (exam_id, _j(flags), q["id"]))

    def assign_exam_to_page(self, page_id: int, exam_id: int) -> None:
        self._adopt_exam(page_id, exam_id)
        self.conn.commit()

    def resolve_exams(self) -> int:
        """Pages without an exam inherit from the immediately preceding printed page, if that is known."""
        changed = 0
        while True:
            n = 0
            for p in self.conn.execute("SELECT id, printed_page FROM pages WHERE exam_id IS NULL AND printed_page IS NOT NULL").fetchall():
                prev = self.exam_of_printed_page(p["printed_page"] - 1)
                if prev is not None:
                    self._adopt_exam(p["id"], prev)
                    n += 1
            self.conn.commit()
            changed += n
            if n == 0:
                return changed

    # ------------------------------------------------------------------ questions
    def _row_to_q(self, r) -> dict:
        d = dict(r)
        for col, name in _JSON_COLS.items():
            d[name] = json.loads(d.pop(col)) if d.get(col) else (None if name in ("alt", "proof", "fact") else [])
        return d

    def get_question(self, qid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return self._row_to_q(r) if r else None

    def find_by_hash(self, h: str):
        r = self.conn.execute("SELECT * FROM questions WHERE content_hash=? AND status!='rejected' ORDER BY id LIMIT 1", (h,)).fetchone()
        return self._row_to_q(r) if r else None

    def near_duplicates(self, norm_stem: str, subject: str, ratio: float, exclude_id: int | None = None):
        out = []
        # Two texts can only reach `ratio` if their lengths are close enough (the same bound SequenceMatcher's
        # real_quick_ratio uses), so the database skips everything else. Keeps this fast with tens of thousands of cards.
        la = len(norm_stem)
        lo, hi = int(la * ratio / (2 - ratio)), int(la * (2 - ratio) / ratio) + 1
        for r in self.conn.execute("SELECT id, norm_stem FROM questions WHERE subject=? AND status!='rejected' "
                                   "AND length(norm_stem) BETWEEN ? AND ?", (subject, lo, hi)):
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            sm = SequenceMatcher(None, norm_stem, r["norm_stem"] or "", autojunk=False)
            if sm.real_quick_ratio() < ratio or sm.quick_ratio() < ratio:
                continue
            rt = sm.ratio()
            if rt >= ratio:
                out.append((r["id"], rt))
        return out

    def _unique_guid(self, h: str) -> str:
        base = hashlib.sha1(h.encode()).hexdigest()[:16]
        guid, i = base, 0
        while self.conn.execute("SELECT 1 FROM questions WHERE guid=?", (guid,)).fetchone():
            i += 1
            guid = f"{base}{i}"
        return guid

    def ingest_question(self, q: dict, *, page_id: int, photo_id: int, exam_id: int | None, printed_page,
                        status: str, near_dup_ratio: float = 0.92) -> tuple[int, str]:
        """Insert a question, or merge it into an identical one already in the database.

        -> (question_id, 'inserted' | 'duplicate')
        """
        h = content_hash(q["stem"], [o["text"] for o in q["options"]])
        existing = self.find_by_hash(h) if q["stem"] else None
        if existing:
            self.conn.execute("INSERT INTO appearances(question_id,exam_id,number,printed_page,photo_id) VALUES(?,?,?,?,?)",
                              (existing["id"], exam_id, q["number"], printed_page, photo_id))
            # same question, different correct answer text -> someone has to look at it
            def ans_text(d):
                lab = d.get("correct_label")
                for o in d["options"]:
                    if lab and o["label"] == lab:
                        return norm_text(o["text"])
                return None
            a_old, a_new = ans_text(existing), ans_text(q)
            if a_old and a_new and a_old != a_new:
                flags = existing["flags"] + [{"code": "answer_conflict", "hard": True,
                    "msg": f"the same question appears again (page {printed_page}, Q{q['number']}) with a different correct answer"}]
                self.update_question(existing["id"], flags=flags, status="review")
            self.conn.commit()
            return existing["id"], "duplicate"

        flags = list(q["flags"])
        nstem = norm_text(q["stem"])
        if nstem:
            for oid, rt in self.near_duplicates(nstem, q["subject"], near_dup_ratio):
                flags.append({"code": "near_duplicate", "hard": False,
                              "msg": f"very similar to question #{oid} (similarity {rt:.2f}); may be the same question reworded"})
        guid = self._unique_guid(h)
        cur = self.conn.execute(
            "INSERT INTO questions(guid,page_id,col_idx,number,exam_id,subject,stem,norm_stem,options_json,correct_label,key_raw,"
            "explanation,ai_explanation,figure_path,crop_path,flags_json,status,content_hash,alt_json,created_at,updated_at,"
            "answer_source,explanation_original,proof_json,fact_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (guid, page_id, q.get("col_idx"), q["number"], exam_id, q["subject"], q["stem"], nstem, _j(q["options"]),
             q.get("correct_label"), q.get("key_raw"), q["explanation"], q.get("ai_explanation"), q.get("figure_path"),
             q.get("crop_path"), _j(flags), status, h, _j(q["alt"]) if q.get("alt") else None,
             now(), now(), q.get("answer_source"),
             q.get("explanation_original"), _j(q["proof"]) if q.get("proof") else None,
             _j(q["fact"]) if q.get("fact") else None))
        qid = cur.lastrowid
        self.conn.execute("INSERT INTO appearances(question_id,exam_id,number,printed_page,photo_id) VALUES(?,?,?,?,?)",
                          (qid, exam_id, q["number"], printed_page, photo_id))
        self.conn.commit()
        return qid, "inserted"

    def update_question(self, qid: int, **fields):
        cols, vals = [], []
        for k, v in fields.items():
            col = {"options": "options_json", "flags": "flags_json", "alt": "alt_json", "proof": "proof_json", "fact": "fact_json"}.get(k, k)
            if col.endswith("_json"):
                v = _j(v) if v is not None else None
            cols.append(f"{col}=?")
            vals.append(v)
        if "stem" in fields:
            cols.append("norm_stem=?")
            vals.append(norm_text(fields["stem"]))
        cols.append("updated_at=?")
        vals.append(now())
        self.conn.execute(f"UPDATE questions SET {', '.join(cols)} WHERE id=?", (*vals, qid))
        self.conn.commit()

    def list_questions(self, where: str = "1=1", params: tuple = (), limit: int | None = None) -> list[dict]:
        """No limit unless asked for: export and fill must see every card."""
        sql = ("SELECT q.*, p.printed_page AS printed_page, ph.stem AS photo_stem FROM questions q "
               "LEFT JOIN pages p ON q.page_id=p.id LEFT JOIN photos ph ON p.photo_id=ph.id "
               f"WHERE {where} ORDER BY IFNULL(p.printed_page, 0), q.col_idx, q.number")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_q(r) for r in rows]

    def appearances(self, qid: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM appearances WHERE question_id=? ORDER BY id", (qid,))]

    # ------------------------------------------------------------------ API usage (for free-tier planning)
    def add_usage(self, day: str, model: str, calls: int, tokens_in: int, tokens_out: int) -> None:
        self.conn.execute(
            "INSERT INTO usage(day,model,calls,input_tokens,output_tokens) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day,model) DO UPDATE SET calls=calls+excluded.calls, "
            "input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens",
            (day, model, calls, tokens_in, tokens_out))
        self.conn.commit()

    def usage_for(self, day: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM usage WHERE day=? ORDER BY model", (day,))]

    # ------------------------------------------------------------------ API key rotation (optional)
    def add_key_usage(self, day: str, key_id: str, model: str, calls: int, exhausted: bool) -> None:
        self.conn.execute(
            "INSERT INTO key_usage(day,key_id,model,calls,exhausted) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day,key_id,model) DO UPDATE SET calls=calls+excluded.calls, "
            "exhausted=MAX(exhausted, excluded.exhausted)", (day, key_id, model, calls, int(exhausted)))
        self.conn.commit()

    def key_usage_today(self, day: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM key_usage WHERE day=?", (day,))]

    def set_key_state(self, key_id: str, state: str, note: str) -> None:
        self.conn.execute("INSERT INTO key_state(key_id,state,note,updated) VALUES(?,?,?,?) "
                          "ON CONFLICT(key_id) DO UPDATE SET state=excluded.state, note=excluded.note, updated=excluded.updated",
                          (key_id, state, note, now()))
        self.conn.commit()

    def key_states(self) -> dict:
        return {r["key_id"]: (r["state"], r["note"] or "") for r in self.conn.execute("SELECT * FROM key_state")}

    def clear_key_states(self) -> None:
        self.conn.execute("DELETE FROM key_state")
        self.conn.commit()

    def stats(self) -> dict:
        s = {}
        for r in self.conn.execute("SELECT status, COUNT(*) c FROM questions GROUP BY status"):
            s[r["status"]] = r["c"]
        s["exported"] = self.conn.execute("SELECT COUNT(*) c FROM questions WHERE exported_at IS NOT NULL").fetchone()["c"]
        s["photos"] = self.conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
        s["failed_photos"] = self.conn.execute("SELECT COUNT(*) c FROM photos WHERE status='failed'").fetchone()["c"]
        s["duplicates_merged"] = self.conn.execute(
            "SELECT COUNT(*) c FROM appearances a WHERE a.id NOT IN (SELECT MIN(id) FROM appearances GROUP BY question_id)").fetchone()["c"]
        return s

"""Data side of the review screen: the queue, one card with its photo crops, saving edits, naming exams."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .textnorm import content_hash


class Review:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db

    # ------------------------------------------------------------------ queries
    def queue(self, flt: str) -> list[dict]:
        if flt == "review":
            qs = self.db.list_questions("q.status='review'")
        elif flt == "soft":
            qs = [q for q in self.db.list_questions("q.status='ok'") if q["flags"]]
        elif flt == "ai":
            qs = [q for q in self.db.list_questions("q.status!='rejected'") if q.get("answer_source") in ("ai", "ai_corrected")]
        elif flt == "disputed":
            codes = {"answer_disputed", "outdated", "explanation_disputed", "text_suspect", "answer_corrected", "explanation_corrected"}
            qs = [q for q in self.db.list_questions("q.status!='rejected'") if any(f["code"] in codes for f in q["flags"])]
        elif flt == "all":
            qs = self.db.list_questions("q.status!='rejected'", limit=3000)
        else:
            qs = self.db.list_questions("q.status=?", (flt,))
        return [{"id": q["id"], "number": q["number"], "page": q["printed_page"], "subject": q["subject"], "status": q["status"],
                 "hard": sum(1 for f in q["flags"] if f["hard"]), "soft": sum(1 for f in q["flags"] if not f["hard"]),
                 "snippet": (q["stem"] or "")[:70], "exam": self.db.exam_display(q["exam_id"])} for q in qs]

    def counts(self) -> dict:
        r = lambda sql: self.db.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {"review": r("SELECT COUNT(*) FROM questions WHERE status='review'"),
                "soft": r("SELECT COUNT(*) FROM questions WHERE status='ok' AND IFNULL(flags_json,'[]') NOT IN ('[]','')"),
                "ai": r("SELECT COUNT(*) FROM questions WHERE answer_source IN ('ai','ai_corrected') AND status!='rejected'"),
                "disputed": r("SELECT COUNT(*) FROM questions WHERE status!='rejected' AND (flags_json LIKE '%answer_disputed%' "
                              "OR flags_json LIKE '%\"outdated\"%' OR flags_json LIKE '%explanation_disputed%' "
                              "OR flags_json LIKE '%text_suspect%' OR flags_json LIKE '%_corrected\"%')"),
                "all": r("SELECT COUNT(*) FROM questions WHERE status!='rejected'")}

    def _url(self, path: Path) -> str | None:
        try:
            rel = Path(path).resolve().relative_to(self.cfg.work.resolve())
        except ValueError:
            return None
        return "/file/" + quote(str(rel).replace("\\", "/"), safe="/")          # '#', '%', spaces in folder names

    def detail(self, qid: int) -> dict | None:
        q = self.db.get_question(qid)
        if not q:
            return None
        page = self.db.conn.execute("SELECT p.idx, p.printed_page, ph.workdir FROM pages p JOIN photos ph ON p.photo_id=ph.id "
                                    "WHERE p.id=?", (q["page_id"],)).fetchone()
        ctx = overlay = None
        if page and page["workdir"]:
            wd = self.cfg.resolve(page["workdir"])
            ctx = self._url(wd / "crops" / f"p{page['idx']}_page.jpg") or self._url(wd / "crops" / f"p{page['idx']}_col0.jpg")
            overlay = self._url(wd / "layout_overlay.jpg")
        q["crop_url"] = self._url(self.cfg.root / q["crop_path"]) if q.get("crop_path") else None
        q["figure_url"] = self._url(self.cfg.root / q["figure_path"]) if q.get("figure_path") else None
        q["context_url"], q["overlay_url"] = ctx, overlay
        q["printed_page"] = page["printed_page"] if page else None
        q["exam_name"] = self.db.exam_display(q["exam_id"])
        q["exams"] = [{"id": e["id"], "name": e["label"] or (e["title"] or "")[:60] or "Unknown"} for e in self.db.exams()]
        q["appearances"] = [{"exam": self.db.exam_display(a["exam_id"]), "number": a["number"], "page": a["printed_page"]}
                            for a in self.db.appearances(qid)]
        q["subjects"] = list(self.cfg.subjects) + ["Other"]
        return q

    # ------------------------------------------------------------------ edits
    def save(self, qid: int, body: dict) -> dict:
        q = self.db.get_question(qid)
        if not q:
            return {"error": "not found"}
        action = body.get("action", "save")
        if action == "reject":
            self.db.update_question(qid, status="rejected")
            return {"ok": True}
        if action == "reopen":
            self.db.update_question(qid, status="review")
            return {"ok": True}
        stem = (body.get("stem") or "").strip()
        options = [{"label": o["label"], "shown": o.get("shown") or o["label"], "text": (o.get("text") or "").strip()}
                   for o in body.get("options", [])]
        correct = body.get("correct_label") or None
        if action == "approve":
            if not stem:
                return {"error": "The question text is empty."}
            if not options or any(not o["text"] for o in options):
                return {"error": "Every option needs text."}
            if correct not in [o["label"] for o in options]:
                return {"error": "Pick the correct answer before approving."}
        fields = dict(stem=stem, options=options, correct_label=correct, explanation=(body.get("explanation") or "").strip(),
                      ai_explanation=(body.get("ai_explanation") or "").strip() or None,
                      subject=body.get("subject") or q["subject"], content_hash=content_hash(stem, [o["text"] for o in options]))
        if body.get("exam_id"):
            fields["exam_id"] = int(body["exam_id"])
        if correct and correct != q.get("correct_label"):
            fields.update(answer_source="user", key_raw="you")     # your answer: no longer labelled as found by the AI
        if action == "approve":
            fields["status"] = "approved"
        self.db.update_question(qid, **fields)
        return {"ok": True}

    def name_exam_for(self, qid: int, label: str) -> dict:
        """Create an exam from a short name and give it to this question's page (and the pages that follow it)."""
        q = self.db.get_question(qid)
        label = (label or "").strip()
        if not q:
            return {"error": "not found"}
        if not label:
            return {"error": "Type a short name for the exam."}
        exam_id = self.db.get_or_create_exam(label, None, {"source": "named in the app"})
        self.db.set_exam_label(exam_id, label)
        self.db.assign_exam_to_page(q["page_id"], exam_id)
        self.db.resolve_exams()
        return {"ok": True, "exam_id": exam_id}

    def set_exam_label(self, exam_id: int, label: str) -> dict:
        self.db.set_exam_label(exam_id, label)
        return {"ok": True}

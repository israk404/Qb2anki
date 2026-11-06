"""AI fill: answers the source does not give, and explanations it does not have.

Everything the AI supplies is labelled so it can never pass for the source:
  * the card says "worked out by AI" and gets the tag Answer::AI
  * an AI answer that is not high-confidence blocks export until you approve it
  * AI explanations are shown only when the source has none
"""
from __future__ import annotations

from . import prompts
from .flags import finalize_status, mkflag
from .schemas import FILL_SCHEMA, FillModel
from .textnorm import canon_label

MISSING_ANSWER_CODES = {"missing_answer", "no_key_entry", "key_unreadable"}
BATCH = 8


def _usable(q: dict) -> bool:
    return bool(q.get("stem")) and len(q.get("options") or []) >= 2


def needs_answer(q: dict) -> bool:
    return (_usable(q) and q.get("correct_label") is None
            and any(f["code"] in MISSING_ANSWER_CODES for f in q.get("flags", [])))


def needs_explanation(q: dict) -> bool:
    return _usable(q) and not (q.get("explanation") or "").strip() and not q.get("ai_explanation")


def _shown(q: dict, label: str | None) -> str | None:
    for o in q["options"]:
        if label and o["label"] == label:
            return o.get("shown") or o["label"]
    return None


def build_jobs(qs: list[dict], size: int = BATCH) -> list[tuple[dict, list[dict]]]:
    """-> [(id -> question, payload)] for every question that needs an answer and/or an explanation."""
    todo = [q for q in qs if needs_answer(q) or needs_explanation(q)]
    jobs = []
    for start in range(0, len(todo), size):
        id_map, payload = {}, []
        for j, q in enumerate(todo[start:start + size]):
            qid = str(start + j)
            id_map[qid] = q
            item = {"id": qid, "subject": q["subject"], "stem": q["stem"],
                    "options": {(o.get("shown") or o["label"]): o["text"] for o in q["options"]},
                    "need_answer": needs_answer(q), "need_explanation": needs_explanation(q)}
            if q.get("correct_label"):
                item["key"] = _shown(q, q["correct_label"])
            payload.append(item)
        jobs.append((id_map, payload))
    return jobs


def run_job(client, model: str, payload: list[dict], label: str) -> FillModel:
    res = client.generate_json(model=model, prompt=prompts.fill_prompt(payload), schema=FILL_SCHEMA,
                               system=prompts.FILL_SYSTEM, label=label)
    return FillModel.model_validate(res)


def apply_job(id_map: dict[str, dict], fm: FillModel) -> None:
    for r in fm.results:
        q = id_map.get(r.id)
        if q is None:
            continue
        want_a, want_e = needs_answer(q), needs_explanation(q)       # decide BEFORE changing anything
        conf = (r.confidence or "").strip().lower()
        conf = conf if conf in ("high", "medium", "low") else "low"
        if want_e and r.explanation.strip():
            q["ai_explanation"] = r.explanation.strip()
            if not want_a and conf != "high":
                q["flags"].append(mkflag("ai_unsure", "the AI is not fully sure about its explanation: check or delete it",
                                         hard=False))
        if want_a:
            lab = canon_label(r.answer) if r.answer and r.answer.strip().lower() not in ("none", "null") else None
            if lab in [o["label"] for o in q["options"]]:
                q["correct_label"] = lab
                q["answer_source"] = "ai"
                q["key_raw"] = "AI"
                q["flags"] = [f for f in q["flags"] if f["code"] not in MISSING_ANSWER_CODES]
                q["flags"].append(mkflag(
                    "ai_answer", f"the source does not give the answer; the AI worked it out (confidence: {conf}). "
                                 + ("Quick check recommended." if conf == "high" else "Please check it."),
                    hard=(conf != "high")))


def fill_database(db, client, model: str, say=lambda *_: None, should_stop=lambda: False) -> dict:
    """AI answers/explanations for cards already stored (the app's 'Fill gaps' button). Approved cards keep their status."""
    rows = db.list_questions("q.status IN ('ok','review','approved')")
    jobs = build_jobs(rows)
    answered = explained = 0
    for i, (id_map, payload) in enumerate(jobs):
        if should_stop():
            say("Stopped.")
            break
        before = {qid: (q.get("correct_label"), bool(q.get("ai_explanation"))) for qid, q in id_map.items()}
        apply_job(id_map, run_job(client, model, payload, f"fill {i}"))
        for qid, q in id_map.items():
            fields = {"flags": q["flags"], "ai_explanation": q.get("ai_explanation")}
            if q.get("correct_label") != before[qid][0]:
                fields.update(correct_label=q["correct_label"], answer_source=q.get("answer_source"), key_raw=q.get("key_raw"))
                answered += 1
            if q.get("ai_explanation") and not before[qid][1]:
                explained += 1
            if q["status"] in ("ok", "review"):
                fields["status"] = finalize_status(q)
            db.update_question(q["id"], **fields)
        say(f"   filled {min((i + 1) * BATCH, sum(len(m) for m, _ in jobs))}/{sum(len(m) for m, _ in jobs)} cards")
    return {"answered": answered, "explained": explained, "candidates": sum(len(m) for m, _ in jobs)}

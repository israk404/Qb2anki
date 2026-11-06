"""Full AI cross-check of every question, using the text only (no image).

For each question the AI checks, in batches of 8 (one request per batch):
  * the question and options: are they a sensible MCQ? Damaged text is repaired by matching it to the known MCQ it must be;
  * the answer: solved independently and compared with the source's answer;
  * whether the fact is outdated (office holders, counts, rankings, ...);
  * the explanation: is it correct? A missing or wrong explanation gets a short AI-written one.
It also answers questions the source leaves unanswered, so it replaces the separate AI-fill step when switched on.

What happens when the AI disagrees with the source is a setting (checks.fact_policy):
  review  the card waits for your decision in Review (default)
  auto    the AI's high-confidence correction is used, labelled on the card; anything less sure still waits for you
  note    a note only; the source is kept and the card is exported
Everything the AI changes keeps the original, so it can be restored in Review.
"""
from __future__ import annotations

from . import mathcheck, prompts
from .fill import MISSING_ANSWER_CODES
from .flags import finalize_status, mkflag
from .proofread import refresh_text_flags
from .schemas import CHECK_SCHEMA, CheckModel
from .textnorm import canon_label, content_hash, similarity

BATCH = 8
POLICIES = ("review", "auto", "note")
_FACT_CODES = {"fact_text_fixed", "text_suspect", "answer_disputed", "answer_corrected", "outdated", "explanation_disputed",
               "explanation_corrected", "ai_answer", "ai_unsure"}


def _usable(q: dict) -> bool:
    return bool(q.get("stem")) and len(q.get("options") or []) >= 2


def _shown(q: dict, label: str | None) -> str | None:
    for o in q["options"]:
        if label and o["label"] == label:
            return o.get("shown") or o["label"]
    return None


def needs_answer(q: dict) -> bool:
    return q.get("correct_label") is None and any(f["code"] in MISSING_ANSWER_CODES for f in q.get("flags", []))


def build_jobs(qs: list[dict], size: int = BATCH) -> list[tuple[dict, list[dict]]]:
    todo = [q for q in qs if _usable(q)]
    jobs = []
    for start in range(0, len(todo), size):
        id_map, payload = {}, []
        for j, q in enumerate(todo[start:start + size]):
            qid = str(start + j)
            id_map[qid] = q
            payload.append({"id": qid, "subject": q.get("subject"), "stem": q["stem"],
                            "options": {(o.get("shown") or o["label"]): o["text"] for o in q["options"]},
                            "source_answer": _shown(q, q.get("correct_label")),
                            "explanation": (q.get("explanation") or "")[:3000],
                            "need_explanation": not (q.get("explanation") or "").strip() and not q.get("ai_explanation")})
        jobs.append((id_map, payload))
    return jobs


def run_job(client, model: str, payload: list[dict], label: str) -> CheckModel:
    res = client.generate_json(model=model, prompt=prompts.check_prompt(payload), schema=CHECK_SCHEMA,
                               system=prompts.CHECK_SYSTEM, label=label)
    return CheckModel.model_validate(res)


def _keep_first_reading(q: dict) -> None:
    if not q.get("proof"):
        q["proof"] = {"stem": q["stem"], "options": [dict(o) for o in q["options"]], "explanation": q.get("explanation") or "",
                      "note": ""}


def _fix_text(q: dict, r) -> list[str]:
    """Apply a repair of damaged question/option text. -> what was repaired."""
    did = []
    stem = (r.fixed_stem or "").strip()
    if stem and stem != q["stem"] and (not q["stem"].strip() or similarity(q["stem"], stem) >= 0.5):
        _keep_first_reading(q)
        q["stem"] = stem
        did.append("question")
    if r.fixed_options:
        shown = [o.get("shown") or o["label"] for o in q["options"]]
        if [(o.label or "").strip() for o in r.fixed_options] == shown:
            for o, n in zip(q["options"], r.fixed_options):
                t = (n.text or "").strip()
                if t and t != o["text"] and (not o["text"].strip() or similarity(o["text"], t) >= 0.4):
                    _keep_first_reading(q)
                    o["text"] = t
                    if "options" not in did:
                        did.append("options")
    return did


def apply_job(id_map: dict[str, dict], cm: CheckModel, policy: str = "review") -> None:
    policy = policy if policy in POLICIES else "review"
    for r in cm.results:
        q = id_map.get(r.id)
        if q is None:
            continue
        q["flags"] = [f for f in q["flags"] if f["code"] not in _FACT_CODES]     # a re-check replaces the previous verdict
        conf = (r.answer_confidence or "").strip().lower()
        conf = conf if conf in ("high", "medium", "low") else "low"
        labels = [o["label"] for o in q["options"]]
        ai = canon_label(r.answer) if r.answer and r.answer.strip().lower() not in ("none", "null") else None
        ai = ai if ai in labels else None
        src = q.get("correct_label")
        fact = {"answer": ai, "answer_shown": _shown(q, ai), "confidence": conf, "agrees": r.agrees,
                "source_answer": src, "source_answer_shown": _shown(q, src),
                "outdated": bool(r.outdated), "outdated_note": (r.outdated_note or "").strip()[:500],
                "explanation_ok": bool(r.explanation_ok), "explanation_issue": (r.explanation_issue or "").strip()[:500],
                "note": (r.note or "").strip()[:300], "text_ok": bool(r.text_ok)}

        # 1. text
        if not r.text_ok:
            did = _fix_text(q, r)
            if did:
                q["flags"].append(mkflag("fact_text_fixed", "the AI repaired damaged " + " and ".join(did)
                                         + " text (the first reading is kept)", hard=False))
                refresh_text_flags(q)
            else:
                q["flags"].append(mkflag("text_suspect", "the AI thinks the question or options are damaged and could not repair "
                                         "them: compare with the photo"))

        # 2. answer
        changed_answer = False
        if src is None and needs_answer(q):
            if ai:
                q["correct_label"], q["answer_source"], q["key_raw"] = ai, "ai", "AI"
                q["flags"] = [f for f in q["flags"] if f["code"] not in MISSING_ANSWER_CODES]
                q["flags"].append(mkflag("ai_answer", f"the source does not give the answer; the AI worked it out "
                                         f"(confidence: {conf}). " + ("Quick check recommended." if conf == "high" else "Please check it."),
                                         hard=(conf != "high")))
        elif src and ai and ai != src:
            msg = f"the AI thinks {_shown(q, ai)} is correct, the source says {_shown(q, src)} (AI confidence: {conf})" \
                  + (f". {fact['note'] or fact['outdated_note']}" if (fact["note"] or fact["outdated_note"]) else "")
            if policy == "auto" and conf == "high":
                q["correct_label"], q["answer_source"] = ai, "ai_corrected"
                changed_answer = True
                q["flags"].append(mkflag("answer_corrected", "answer corrected by the AI: " + msg, hard=False))
            else:
                q["flags"].append(mkflag("answer_disputed", msg, hard=(policy != "note")))
        elif src and not ai and conf != "low" and r.agrees == "no":
            q["flags"].append(mkflag("answer_disputed", "the AI thinks none of the options is correct"
                                     + (f": {fact['note']}" if fact["note"] else ""), hard=(policy != "note")))

        # 3. outdated
        if r.outdated:
            note = fact["outdated_note"] or "this fact may have changed since the exam"
            fixed_by_ai = changed_answer
            q["flags"].append(mkflag("outdated", "may be outdated: " + note, hard=(policy == "review" and not fixed_by_ai)))

        # 4. explanation
        expl = (r.explanation or "").strip()
        has_source_expl = bool((q.get("explanation") or "").strip())
        if not has_source_expl and expl and not q.get("ai_explanation"):
            q["ai_explanation"] = expl
            if conf != "high":
                q["flags"].append(mkflag("ai_unsure", "the AI is not fully sure about its explanation: check or delete it", hard=False))
        elif has_source_expl and not r.explanation_ok:
            issue = fact["explanation_issue"] or "the explanation does not seem correct"
            if policy == "auto" and conf == "high" and expl and len(mathcheck.hard_problems(expl)) == 0:
                if not q.get("explanation_original"):
                    q["explanation_original"] = q["explanation"]
                q["explanation"] = expl
                q["flags"].append(mkflag("explanation_corrected", f"explanation replaced by the AI: {issue} (the original is kept)",
                                         hard=False))
                refresh_text_flags(q)
            else:
                if expl:
                    q["ai_explanation"] = expl                        # a suggestion to compare with in Review
                q["flags"].append(mkflag("explanation_disputed", f"the AI thinks the explanation is wrong: {issue}",
                                         hard=(policy != "note")))
        q["fact"] = fact
        q["_fact_changed"] = True


def check_database(cfg, db, client, model: str, say=lambda *_: None, should_stop=lambda: False, recheck: bool = False) -> dict:
    """The app's 'Cross-check stored cards' button. Approved and rejected cards are not touched; cards checked before are skipped
    unless `recheck`. A card whose answer or text changed and that was exported before becomes exportable again."""
    where = "q.status IN ('ok','review')" + ("" if recheck else " AND q.fact_json IS NULL")
    rows = db.list_questions(where)
    jobs = build_jobs(rows)
    policy = cfg.get("checks", "fact_policy", "review")
    total = sum(len(m) for m, _ in jobs)
    disputed = 0
    for i, (id_map, payload) in enumerate(jobs):
        if should_stop():
            say("Stopped.")
            break
        before = {qid: (q.get("correct_label"), q["stem"], [o["text"] for o in q["options"]], q.get("explanation"))
                  for qid, q in id_map.items()}
        apply_job(id_map, run_job(client, model, payload, f"check {i}"), policy)
        for qid, q in id_map.items():
            if not q.pop("_fact_changed", False):
                continue
            fields = dict(stem=q["stem"], options=q["options"], explanation=q.get("explanation") or "", flags=q["flags"],
                          fact=q["fact"], proof=q.get("proof"), ai_explanation=q.get("ai_explanation"),
                          explanation_original=q.get("explanation_original"), correct_label=q.get("correct_label"),
                          answer_source=q.get("answer_source"), key_raw=q.get("key_raw"),
                          content_hash=content_hash(q["stem"], [o["text"] for o in q["options"]]), status=finalize_status(q))
            if (q.get("correct_label"), q["stem"], [o["text"] for o in q["options"]], q.get("explanation")) != before[qid] \
                    and q.get("exported_at"):
                fields["exported_at"] = None
            if any(f["hard"] and f["code"] in _FACT_CODES for f in q["flags"]):
                disputed += 1
            db.update_question(q["id"], **fields)
        say(f"   checked {min((i + 1) * BATCH, total)}/{total} cards")
    return {"checked": total, "held": disputed}

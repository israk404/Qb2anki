"""AI proofreading: each page's transcription is compared with the page picture and reading errors are corrected.

It fixes misread letters and digits (Bengali ৪ read as 8, ...), broken or misplaced LaTeX, ditto marks in worked solutions, and text
attached to the wrong option. One request per page. Every correction is checked before it is accepted (it must look like a
correction, not a rewrite), and the original reading is kept on the card so it can be restored in Review.
"""
from __future__ import annotations

from . import imaging, mathcheck, prompts
from .flags import finalize_status, mkflag
from .schemas import PROOF_SCHEMA, ProofModel
from .textnorm import content_hash, similarity

_TEXT_CODES = {"empty_stem", "empty_option", "unreadable_text", "latex", "latex_style"}


def payload_for(qs: list[dict]) -> tuple[dict, list[dict]]:
    id_map, payload = {}, []
    for i, q in enumerate(qs):
        if not q.get("stem") and not q.get("options"):
            continue
        qid = str(i)
        id_map[qid] = q
        payload.append({"id": qid, "number": q.get("number_raw") or q.get("number"), "stem": q["stem"],
                        "options": [{"label": o.get("shown") or o["label"], "text": o["text"]} for o in q["options"]],
                        "explanation": q.get("explanation") or ""})
    return id_map, payload


def run(client, model: str, image, payload: list[dict], label: str) -> ProofModel:
    res = client.generate_json(model=model, prompt=prompts.proofread_prompt(payload), schema=PROOF_SCHEMA, image=image,
                               system=prompts.PROOF_SYSTEM, label=label)
    return ProofModel.model_validate(res)


def _ok(old: str, new: str, floor: float) -> bool:
    """Is `new` a correction of `old` (not a different text)?"""
    if new is None or new == old:
        return False
    if not (old or "").strip():
        return bool(new.strip())              # text that was missing although it is printed
    if not new.strip():
        return False                           # never delete text
    return similarity(old, new) >= floor


def refresh_text_flags(q: dict) -> None:
    """Recompute the flags that depend only on the text, after the text changed."""
    q["flags"] = [f for f in q["flags"] if f["code"] not in _TEXT_CODES]
    if not q["stem"]:
        q["flags"].append(mkflag("empty_stem", "question text is empty"))
    for o in q["options"]:
        if not o["text"]:
            q["flags"].append(mkflag("empty_option", f"option {o.get('shown') or o['label']} is empty"))
    blob = " ".join([q["stem"], q.get("explanation") or ""] + [o["text"] for o in q["options"]])
    if "[?]" in blob:
        q["flags"].append(mkflag("unreadable_text", "the model marked characters as unreadable ([?])"))
    for hard, msg in mathcheck.lint(blob):
        q["flags"].append(mkflag("latex" if hard else "latex_style", msg, hard=hard))


def apply(id_map: dict[str, dict], pm: ProofModel) -> int:
    """-> number of cards changed."""
    changed = 0
    for r in pm.results:
        q = id_map.get(r.id)
        if q is None:
            continue
        before = {"stem": q["stem"], "options": [dict(o) for o in q["options"]], "explanation": q.get("explanation") or ""}
        did = []
        if _ok(q["stem"], r.stem, 0.6):
            q["stem"] = r.stem.strip()
            did.append("question")
        if r.options:
            shown = [o.get("shown") or o["label"] for o in q["options"]]
            new_labels = [(o.label or "").strip() for o in r.options]
            if new_labels == shown:                                   # same options, same order: compare text by text
                for o, n in zip(q["options"], r.options):
                    if _ok(o["text"], n.text, 0.5):
                        o["text"] = n.text.strip()
                        if "options" not in did:
                            did.append("options")
        if r.explanation is not None and _ok(before["explanation"], r.explanation, 0.45) and \
                len(mathcheck.hard_problems(r.explanation)) <= len(mathcheck.hard_problems(before["explanation"])):
            q["explanation"] = r.explanation.strip()
            did.append("explanation")
        if not did:
            continue
        changed += 1
        q["_proof_changed"] = True
        if not q.get("proof"):                      # keep the FIRST reading if a card is proofread more than once
            q["proof"] = {**before, "note": ""}
        q["proof"]["note"] = (r.changes or "").strip()[:300]
        q["flags"] = [f for f in q["flags"] if f["code"] != "proofread"]
        q["flags"].append(mkflag("proofread", "AI proofreading corrected the " + ", ".join(did)
                                 + (f": {q['proof']['note']}" if q["proof"]["note"] else "") + " (the first reading is kept)", hard=False))
        refresh_text_flags(q)
    return changed


def proofread_database(cfg, db, client, model: str, say=lambda *_: None, should_stop=lambda: False) -> dict:
    """The app's 'Proofread stored cards' button: proofread cards read before (approved and rejected cards are not touched).
    A corrected card that was exported before becomes exportable again, so importing with 'Update' refreshes it in Anki."""
    rows = db.conn.execute(
        "SELECT q.page_id, p.idx, ph.workdir FROM questions q JOIN pages p ON q.page_id=p.id JOIN photos ph ON p.photo_id=ph.id "
        "WHERE q.status IN ('ok','review') GROUP BY q.page_id ORDER BY q.page_id").fetchall()
    pages = changed = skipped = 0
    for r in rows:
        if should_stop():
            say("Stopped.")
            break
        wd = cfg.resolve(r["workdir"]) if r["workdir"] else None
        img_path = next((p for p in (wd / "crops" / f"p{r['idx']}_page.jpg", wd / "crops" / f"p{r['idx']}_col0.jpg") if p.exists()), None) \
            if wd else None
        if img_path is None:
            skipped += 1
            continue
        qs = db.list_questions("q.page_id=? AND q.status IN ('ok','review')", (r["page_id"],))
        id_map, payload = payload_for(qs)
        if not payload:
            continue
        n = apply(id_map, run(client, model, imaging.load_image(img_path), payload, f"proof page {r['page_id']}"))
        pages += 1
        for q in id_map.values():
            if not q.pop("_proof_changed", False):          # only cards changed in THIS run are written
                continue
            fields = dict(stem=q["stem"], options=q["options"], explanation=q["explanation"], flags=q["flags"], proof=q["proof"],
                          content_hash=content_hash(q["stem"], [o["text"] for o in q["options"]]), status=finalize_status(q))
            if q.get("exported_at"):
                fields["exported_at"] = None
            db.update_question(q["id"], **fields)
        changed += n
        say(f"   page {pages}/{len(rows)}: {n} correction(s)")
    return {"pages": pages, "changed": changed, "skipped": skipped}

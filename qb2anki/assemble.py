"""Turn the raw reading of one page into validated question dicts with explicit flags (see flags.py).

Nothing is silently trusted: structure problems, answer-key problems, unreadable text and (optionally) disagreement between
two independent readings all become flags.
"""
from __future__ import annotations

from . import mathcheck
from .flags import mkflag
from .schemas import KeyEntry, PageModel, QuestionModel
from .textnorm import BN_LABELS, canon_key_answer, canon_label, display_label, parse_number, similarity


# --------------------------------------------------------------------------- normalise
def normalize_question(qm: QuestionModel, subjects: dict) -> dict:
    opts = []
    for o in qm.options:
        lab = canon_label(o.label) or (o.label or "").strip()
        opts.append({"label": lab, "shown": display_label(o.label, lab), "text": mathcheck.tidy((o.text or "").strip())})
    labels = [o["label"] for o in opts]
    if len(set(labels)) == len(labels) and all(l in BN_LABELS for l in labels):
        opts.sort(key=lambda o: BN_LABELS.index(o["label"]))
    return {
        "number": parse_number(qm.number),
        "number_raw": qm.number,
        "col_idx": 0,
        "stem": mathcheck.tidy((qm.stem or "").strip()),
        "options": opts,
        "explanation": mathcheck.tidy((qm.explanation or "").strip()),
        "ans_in_expl": canon_label(qm.answer_in_explanation) if qm.answer_in_explanation else None,
        "subject": qm.subject if qm.subject in subjects else "Other",
        "has_figure": bool(qm.has_figure),
        "figure_box": qm.figure_box,
        "question_box": qm.question_box,
        "truncated_top": bool(qm.truncated_top),
        "truncated_bottom": bool(qm.truncated_bottom),
        "flags": [],
        "alt": None,
    }


def _by_number(qs: list[dict]) -> tuple[dict, list[int]]:
    seen: dict = {}
    dups: list[int] = []
    for q in qs:
        n = q["number"]
        if n is None:
            continue
        if n in seen:
            dups.append(n)
        else:
            seen[n] = q
    return seen, dups


def _neighbours(n: int, numbers: list[int]) -> list[int]:
    """The questions printed just before and after number n (where a problem 'is near')."""
    lower = [x for x in numbers if x < n]
    higher = [x for x in numbers if x > n]
    return ([lower[-1]] if lower else []) + ([higher[0]] if higher else [])


# --------------------------------------------------------------------------- cross-check between two readings
def compare_questions(qa: dict, qb: dict, th: dict) -> list[dict]:
    flags = []
    s = similarity(qa["stem"], qb["stem"])
    if s < th["stem"]:
        flags.append(mkflag("extract_disagree_stem", f"question text differs between the two readings (similarity {s:.2f})"))
    la, lb = [o["label"] for o in qa["options"]], [o["label"] for o in qb["options"]]
    if la != lb:
        flags.append(mkflag("extract_disagree_options", f"option labels differ between readings: {la} vs {lb}"))
    else:
        for oa, ob in zip(qa["options"], qb["options"]):
            s = similarity(oa["text"], ob["text"])
            if s < th["option"]:
                flags.append(mkflag("extract_disagree_option", f"option {oa['shown']} differs between readings (similarity {s:.2f})"))
    s = similarity(qa["explanation"], qb["explanation"])
    if s < th["explanation"]:
        flags.append(mkflag("extract_disagree_explanation", f"explanation differs between readings (similarity {s:.2f})", hard=False))
    if qa["ans_in_expl"] != qb["ans_in_expl"]:
        flags.append(mkflag("extract_disagree_answer", "the two readings disagree on the answer printed in the source", hard=False))
    return flags


def merge_passes(a: list[dict], b: list[dict] | None, th: dict):
    """-> (questions, page_flags). Reading A is the one stored; reading B only cross-checks it."""
    page_flags: list[dict] = []
    a_map, a_dups = _by_number(a)
    for n in a_dups:
        page_flags.append({**mkflag("duplicate_number_on_page", f"question number {n} was read twice on this page"), "near": [n]})
    b_map = _by_number(b)[0] if b is not None else {}
    for q in a:
        if b is not None and q["number"] is not None:
            qb = b_map.get(q["number"])
            if qb is None:
                q["flags"].append(mkflag("missing_in_second_pass", "the second reading did not find this question"))
            else:
                q["flags"].extend(compare_questions(q, qb, th))
                q["alt"] = {"stem": qb["stem"], "options": qb["options"], "explanation": qb["explanation"], "ans_in_expl": qb["ans_in_expl"]}
    if b is not None:
        for n in sorted(set(b_map) - set(a_map)):
            page_flags.append(mkflag("missing_in_first_pass", f"the second reading found question {n} but the first did not"))
    return a, page_flags


# --------------------------------------------------------------------------- answer key
def parse_key(entries: list[KeyEntry]) -> dict[int, str | None]:
    """number -> 'ক'/'খ'/'গ'/'ঘ'/'ঙ' | 'NOTE' | None (unreadable)."""
    out: dict[int, str | None] = {}
    for e in entries:
        n = parse_number(e.number)
        if n is not None:
            out[n] = canon_key_answer(e.answer)
    return out


def merge_keys(ka: dict, kb: dict | None) -> dict[int, dict]:
    res: dict[int, dict] = {}
    for n in sorted(set(ka) | set(kb or {})):
        a = ka.get(n, "<absent>")
        b = kb.get(n, "<absent>") if kb is not None else a
        if kb is not None and a != b:
            res[n] = {"status": "disagree", "a": a, "b": b}
        elif a is None:
            res[n] = {"status": "unreadable"}
        elif a == "<absent>":
            continue
        elif a == "NOTE":
            res[n] = {"status": "note"}
        else:
            res[n] = {"status": "ok", "label": a}
    return res


def _show(v) -> str:
    return "—" if v in (None, "<absent>") else str(v)


# --------------------------------------------------------------------------- per-question validation
def validate_question(q: dict, checks: dict, key: dict[int, dict], page_has_key: bool) -> None:
    f = q["flags"]
    if not q["stem"]:
        f.append(mkflag("empty_stem", "question text is empty"))
    if q["number"] is None:
        f.append(mkflag("no_number", f"could not read the question number ('{q['number_raw']}')"))
    allowed = checks.get("allowed_option_counts", [4, 5])
    if len(q["options"]) not in allowed:
        f.append(mkflag("option_count", f"{len(q['options'])} options found (expected {allowed})"))
    labels = [o["label"] for o in q["options"]]
    if len(set(labels)) != len(labels) or any(l not in BN_LABELS for l in labels):
        f.append(mkflag("option_labels", f"unexpected option labels {labels}"))
    for o in q["options"]:
        if not o["text"]:
            f.append(mkflag("empty_option", f"option {o['shown']} is empty"))
    blob = " ".join([q["stem"], q["explanation"]] + [o["text"] for o in q["options"]])
    if "[?]" in blob:
        f.append(mkflag("unreadable_text", "the model marked characters as unreadable ([?])"))
    for hard, msg in mathcheck.lint(blob):
        f.append(mkflag("latex" if hard else "latex_style", msg, hard=hard))
    if q["truncated_top"] or q["truncated_bottom"]:
        f.append(mkflag("truncated", "question is cut off at the edge of the picture"))
    if q["has_figure"]:
        f.append(mkflag("figure_check", "has a figure: check the cropped figure image"))

    correct = None
    n = q["number"]
    entry = key.get(n) if n is not None else None
    if not page_has_key:
        if q["ans_in_expl"]:
            correct = q["ans_in_expl"]
            f.append(mkflag("answer_from_explanation",
                            "no separate answer key; the answer printed in the source for this question was used", hard=False))
            q["answer_source"] = "printed"
        else:
            f.append(mkflag("missing_answer", "the source gives no answer for this question"))
    elif entry is None:
        f.append(mkflag("no_key_entry", "the answer key has no row for this question"))
    elif entry["status"] == "disagree":
        f.append(mkflag("key_disagree", f"the two key readings disagree: A={_show(entry['a'])}, B={_show(entry['b'])}"))
    elif entry["status"] == "unreadable":
        f.append(mkflag("key_unreadable", "the answer-key cell could not be read"))
    elif entry["status"] == "note":
        if q["ans_in_expl"]:
            correct = q["ans_in_expl"]
            q["answer_source"] = "note"
            f.append(mkflag("answer_from_note", "key says 'Note'; answer taken from the explanation", hard=False))
        else:
            f.append(mkflag("missing_answer", "key says 'Note' but the explanation does not name an option"))
    else:
        correct = entry["label"]
        q["answer_source"] = "key"
        if q["ans_in_expl"] and q["ans_in_expl"] != correct:
            f.append(mkflag("key_vs_explanation", f"key says {correct} but the explanation names {q['ans_in_expl']}"))

    if correct is not None and correct not in labels:
        f.append(mkflag("answer_not_in_options", f"answer {correct} is not among the options {labels}"))
        correct = None
    q["correct_label"] = correct
    q.setdefault("answer_source", None)
    q["key_raw"] = None if entry is None else (entry.get("label") or entry["status"])


# --------------------------------------------------------------------------- one page
def assemble_page(page_a: PageModel, page_b: PageModel | None, subjects: dict, checks: dict):
    """-> (questions, page_flags). `page_b` is the optional second reading (cross-check)."""
    th = {"stem": checks.get("stem_similarity", 0.97), "option": checks.get("option_similarity", 0.97),
          "explanation": checks.get("explanation_similarity", 0.95)}
    qa = [normalize_question(q, subjects) for q in page_a.questions]
    qb = [normalize_question(q, subjects) for q in page_b.questions] if page_b is not None else None
    questions, page_flags = merge_passes(qa, qb, th)

    has_key = bool(page_a.answer_key)
    key = merge_keys(parse_key(page_a.answer_key), parse_key(page_b.answer_key) if page_b is not None else None) if has_key else {}
    for q in questions:
        validate_question(q, checks, key, has_key)

    got = {q["number"] for q in questions if q["number"] is not None}
    if has_key and key:
        for n in sorted(set(key) - got):
            page_flags.append({**mkflag("question_missing",
                                        f"the answer key lists question {n} but no question {n} was extracted "
                                        f"(look near the questions before and after it on the photo)"),
                               "near": _neighbours(n, sorted(got))})
    nums = sorted(got)
    if nums:
        missing = [n for n in range(nums[0], nums[-1] + 1) if n not in got and n not in set(key)]
        if missing:
            page_flags.append({**mkflag("number_gap", f"question numbers {missing} are missing from the page", hard=False),
                               "near": sorted({x for n in missing for x in _neighbours(n, nums)})})
    return questions, page_flags

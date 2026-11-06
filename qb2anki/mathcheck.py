"""Math quality: a free formula lint, plus an AI check/repair of worked solutions.

Pipeline for every math explanation:
  1. lint()            local, free: does each formula even render? (balanced braces, \frac arguments, ...)
  2. AI check (batch)  repairs formatting only; says whether the working reaches the book's answer
  3. AI re-read        only for what is still broken: the question's picture is read again, focused on the solution
Anything the AI changed keeps the original text (explanation_original) so you can compare in the review tool.
"""
from __future__ import annotations

import re

from . import prompts
from .flags import mkflag
from .schemas import MATH_CHECK_SCHEMA, REREAD_SCHEMA, MathModel, RereadModel
from .textnorm import similarity

_MATH_RE = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", re.S)
_BANGLA = re.compile(r"[\u0980-\u09FF]")
_TEXT_GROUP = re.compile(r"\\(?:text|mathrm|textbf|mbox)\s*\{[^{}]*\}")
_LATEX_CODES = {"latex", "latex_style"}
BATCH = 6


def segments(text: str) -> list[str]:
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _MATH_RE.finditer(text or "")]


def _group_end(s: str, i: int) -> int:
    """s[i] == '{' -> index just past its matching '}', or -1."""
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _skip_args(s: str, i: int, count: int) -> bool:
    """Do `count` arguments (a {group} or one token) follow position i?"""
    for _ in range(count):
        while i < len(s) and s[i] == " ":
            i += 1
        if i >= len(s):
            return False
        if s[i] == "{":
            i = _group_end(s, i)
            if i < 0:
                return False
        elif s[i] == "\\":
            m = re.match(r"\\[A-Za-z]+|\\.", s[i:])
            i += len(m.group(0)) if m else 1
        else:
            i += 1
    return True


def _lint_segment(seg: str) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    depth = 0
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                break
        i += 1
    if depth != 0:
        out.append((True, "unbalanced braces { } in a formula"))
    for m in re.finditer(r"\\(?:d|t)?frac|\\binom", seg):
        if not _skip_args(seg, m.end(), 2):
            out.append((True, "\\frac is missing an argument"))
            break
    for m in re.finditer(r"\\sqrt", seg):
        j = m.end()
        if j < len(seg) and seg[j] == "[":
            k = seg.find("]", j)
            j = k + 1 if k >= 0 else j
        if not _skip_args(seg, j, 1):
            out.append((True, "\\sqrt is missing its argument"))
            break
    if len(re.findall(r"\\left\b", seg)) != len(re.findall(r"\\right\b", seg)):
        out.append((True, "\\left / \\right do not match"))
    if sorted(re.findall(r"\\begin\{(\w+\*?)\}", seg)) != sorted(re.findall(r"\\end\{(\w+\*?)\}", seg)):
        out.append((True, "\\begin / \\end do not match"))
    if re.search(r"[\^_]\s*$", seg.strip()):
        out.append((True, "a formula ends with ^ or _"))
    if _BANGLA.search(_TEXT_GROUP.sub("", seg)):
        out.append((False, "Bangla words inside a formula (they belong outside the math delimiters)"))
    return out


def lint(text: str) -> list[tuple[bool, str]]:
    """-> [(is_hard, message)] problems that stop a formula from rendering (hard) or look wrong (soft)."""
    text = text or ""
    out: list[tuple[bool, str]] = []
    if text.count("\\(") != text.count("\\)"):
        out.append((True, "unbalanced \\( \\) math delimiters"))
    if text.count("\\[") != text.count("\\]"):
        out.append((True, "unbalanced \\[ \\] math delimiters"))
    for seg in segments(text):
        for item in _lint_segment(seg):
            if item not in out:
                out.append(item)
    bare = _MATH_RE.sub(" ", text)
    if re.search(r"\\(?:frac|sqrt|therefore|times|cdot|div|Rightarrow)\b", bare):
        out.append((False, "a LaTeX command sits outside math delimiters"))
    return out


_STRAY = re.compile(r"\\(?:therefore|because|Rightarrow|Leftarrow|Leftrightarrow|rightarrow|leftarrow|implies|times|div|cdot|"
                    r"pm|mp|leq|geq|le|ge|neq|ne|approx|equiv|infty|pi|theta|alpha|beta|gamma|delta|angle|triangle|circ|degree|"
                    r"perp|parallel|cong|sim|propto|sum|int)(?![A-Za-z])")


def tidy(text: str) -> str:
    """Free, safe repair: LaTeX commands written OUTSIDE the math delimiters (they would show as raw text in Anki) are wrapped
    in \\( ... \\). Simple commands only (\\therefore, \\times, \\pi, ...), plus \\frac{..}{..} and \\sqrt{..} with balanced braces.
    Text inside delimiters is never touched."""
    if not text or "\\" not in text:
        return text or ""
    out, pos = [], 0
    for m in _MATH_RE.finditer(text):
        out.append(_tidy_bare(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_tidy_bare(text[pos:]))
    return "".join(out)


def _tidy_bare(seg: str) -> str:
    res, i = [], 0
    while i < len(seg):
        m = re.match(r"\\(?:d|t)?frac|\\sqrt", seg[i:])
        if m:
            j, count = i + len(m.group(0)), (1 if "sqrt" in m.group(0) else 2)
            ok, k = True, j
            for _ in range(count):
                while k < len(seg) and seg[k] == " ":
                    k += 1
                if k < len(seg) and seg[k] == "{":
                    end = _group_end(seg, k)
                    if end < 0:
                        ok = False
                        break
                    k = end
                else:
                    ok = False
                    break
            if ok:
                res.append("\\(" + seg[i:k] + "\\)")
                i = k
                continue
        m = _STRAY.match(seg, i)
        if m:
            res.append("\\(" + m.group(0) + "\\)")
            i = m.end()
            continue
        res.append(seg[i])
        i += 1
    return "".join(res)


def hard_problems(text: str) -> list[str]:
    return [m for hard, m in lint(text) if hard]


def refresh_latex_flags(q: dict) -> None:
    """Re-run the lint over everything on the card and rewrite its 'latex' flags."""
    q["flags"] = [f for f in q["flags"] if f["code"] not in _LATEX_CODES]
    blob = " ".join([q["stem"], q.get("explanation") or ""] + [o["text"] for o in q["options"]])
    for hard, msg in lint(blob):
        q["flags"].append(mkflag("latex" if hard else "latex_style", msg, hard=hard))


# --------------------------------------------------------------------------- AI check (text only, batched)
def has_math(q: dict) -> bool:
    expl = q.get("explanation") or ""
    return bool(expl.strip()) and (q.get("subject") == "Math" or "\\(" in expl or "\\[" in expl)


def build_check_jobs(qs: list[dict], size: int = BATCH) -> list[tuple[dict, list[dict]]]:
    todo = [q for q in qs if has_math(q)]
    jobs = []
    for start in range(0, len(todo), size):
        id_map, payload = {}, []
        for j, q in enumerate(todo[start:start + size]):
            qid = str(start + j)
            id_map[qid] = q
            item = {"id": qid, "stem": q["stem"], "options": {(o.get("shown") or o["label"]): o["text"] for o in q["options"]},
                    "explanation": q["explanation"]}
            if q.get("correct_label"):
                item["key"] = next((o.get("shown") or o["label"] for o in q["options"] if o["label"] == q["correct_label"]), None)
            payload.append(item)
        jobs.append((id_map, payload))
    return jobs


def run_check(client, model: str, payload: list[dict], label: str) -> MathModel:
    res = client.generate_json(model=model, prompt=prompts.math_check_prompt(payload), schema=MATH_CHECK_SCHEMA,
                               system=prompts.MATH_SYSTEM, label=label)
    return MathModel.model_validate(res)


def _accept_fix(orig: str, fixed: str) -> bool:
    """A repair is accepted only if it is a repair: no more broken than before and still the same text, not a rewrite."""
    return bool(fixed.strip()) and fixed != orig and len(hard_problems(fixed)) <= len(hard_problems(orig)) \
        and similarity(orig, fixed) >= 0.7


def _replace_explanation(q: dict, new: str, code: str, msg: str) -> None:
    if not q.get("explanation_original"):          # keep the FIRST reading (cards loaded from the database carry None)
        q["explanation_original"] = q["explanation"]
    q["explanation"] = new.strip()
    q["flags"].append(mkflag(code, msg, hard=False))
    refresh_latex_flags(q)


def apply_check(id_map: dict[str, dict], mm: MathModel) -> None:
    for r in mm.results:
        q = id_map.get(r.id)
        if q is None:
            continue
        if _accept_fix(q["explanation"], r.fixed_explanation):
            _replace_explanation(q, r.fixed_explanation, "math_fixed",
                                 "the AI repaired the formula formatting of the explanation; the original text is kept for comparison")
        if not r.consistent:
            q["flags"].append(mkflag("math_inconsistent", "the working does not seem to reach the answer"
                                     + (f": {r.note}" if r.note else ""), hard=True))


# --------------------------------------------------------------------------- AI re-read (vision, only for what is still broken)
def still_broken(q: dict) -> list[str]:
    probs = hard_problems(q.get("explanation") or "")
    if any(f["code"] == "math_inconsistent" for f in q["flags"]):
        probs.append("the steps did not seem to reach the answer")
    return probs


def run_reread(client, model: str, image, problems: list[str], label: str) -> RereadModel:
    res = client.generate_json(model=model, prompt=prompts.math_reread_prompt(problems), schema=REREAD_SCHEMA, image=image,
                               system=prompts.SYSTEM, label=label)
    return RereadModel.model_validate(res)


def apply_reread(q: dict, rm: RereadModel) -> bool:
    new = rm.explanation.strip()
    old = q.get("explanation") or ""
    if not new or new == old:
        return False
    better = len(hard_problems(new)) < len(hard_problems(old))
    if not better and any(f["code"] == "math_inconsistent" for f in q["flags"]) and not hard_problems(old):
        better = similarity(old, new) < 0.97          # formatting was fine; the reading itself may have been wrong
    if not better:
        return False
    _replace_explanation(q, new, "math_reread", "the solution was read again from the picture; the first reading is kept for comparison")
    return True

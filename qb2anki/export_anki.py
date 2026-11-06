"""Anki export.

Writes a tab-separated text file that Anki imports directly (File > Import):
    guid | notetype | deck | Front | Back | tags
The built-in "Basic" note type is used, so nothing has to be set up in Anki. The card design lives in the cards themselves
(a small <style> block in the Front field), so it works on desktop and phone, in light and night mode.
Front = subject + question + figure + options. Back (shown under the front by Basic) = the correct answer, then the explanation.

Tap to answer (anki.tap_to_answer): a small script in the Front field makes the options tappable. Tapping one colours it
(green right, red wrong, the right one always green) and turns the card over, where the same colours and the explanation show
above Anki's Again / Hard / Good / Easy buttons. The card turns over by itself on Anki desktop (pycmd) and AnkiDroid
(showAnswer); elsewhere the colours show at once and Show Answer is pressed as usual. Without scripts the card works as before.
"""
from __future__ import annotations

import csv
import html
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .db import now
from .mathcheck import tidy
from .textnorm import slug_tag

HEADER = ["#separator:tab", "#html:true", "#guid column:1", "#notetype column:2", "#deck column:3", "#tags column:6"]

CSS = (
    '.qb{--tx:#1f2933;--mu:#6b7280;--bd:#d8d3c4;--bg:#fbf9f3;--ac:#0f6b57;--acbg:#e4f3ec;--ex:#f1eee4;max-width:40em;margin:0 auto;'
    'text-align:left;font-family:"Noto Sans Bengali","Nirmala UI",Kalpurush,SolaimanLipi,"Segoe UI",system-ui,sans-serif;'
    'font-size:.95em;line-height:1.7;color:var(--tx)}'
    '.nightMode .qb,.night_mode .qb{--tx:#e7e5de;--mu:#9aa0a6;--bd:#3b3f44;--bg:#25282b;--ac:#58c4a3;--acbg:#173a30;--ex:#202326}'
    '.qb-chip{display:inline-block;font-size:.72em;padding:.1em .75em;border-radius:99px;border:1px solid var(--bd);color:var(--mu);margin-bottom:.6em}'
    '.qb-q{font-size:1.12em;font-weight:600;margin:.1em 0 .7em}'
    '.qb-fig img{max-width:100%;border-radius:8px;border:1px solid var(--bd);background:#fff;margin:.2em 0 .7em}'
    '.qb-opt,.qb-ans{display:flex;gap:.7em;align-items:flex-start;padding:.5em .8em;margin:.35em 0;border:1px solid var(--bd);'
    'border-radius:10px;background:var(--bg)}'
    '.qb-l{flex:none;min-width:1.75em;height:1.75em;line-height:1.75em;text-align:center;border-radius:50%;background:var(--ex);'
    'font-weight:700;font-size:.85em}'
    '.qb-t{flex:1;min-width:0}'
    '.qb-ans{border:1.5px solid var(--ac);background:var(--acbg);font-weight:600;margin:.2em 0 .8em;padding:.6em .85em}'
    '.qb-ans .qb-l{background:var(--ac);color:#fff}'
    '.nightMode .qb-ans .qb-l,.night_mode .qb-ans .qb-l{color:#0e211b}'
    '.qb-exp{padding:.7em .95em;border-left:4px solid var(--ac);background:var(--ex);border-radius:0 10px 10px 0;margin:.5em 0}'
    '.qb-note{font-size:.8em;color:var(--mu);margin:.3em 0}'
    '.qb-src{font-size:.78em;color:var(--mu);margin-top:.9em}'
)

TAP_CSS = (
    '.qb-live .qb-opt{cursor:pointer;-webkit-tap-highlight-color:transparent}'
    '.qb-live .qb-opt:hover{border-color:var(--ac)}'
    '.qb .qb-opt.qb-ok{border:1.5px solid #1f9d62;background:#e2f4ea;font-weight:600}'
    '.qb .qb-opt.qb-bad{border:1.5px solid #d14343;background:#fbe6e6}'
    '.qb-ok .qb-l{background:#1f9d62;color:#fff}.qb-bad .qb-l{background:#d14343;color:#fff}'
    '.qb-ok .qb-t:after{content:"  \\2713";color:#1f9d62;font-weight:700}'
    '.qb-bad .qb-t:after{content:"  \\2717";color:#d14343;font-weight:700}'
    '.nightMode .qb .qb-opt.qb-ok,.night_mode .qb .qb-opt.qb-ok{background:#16382a;border-color:#3fbf85}'
    '.nightMode .qb .qb-opt.qb-bad,.night_mode .qb .qb-opt.qb-bad{background:#3d1f20;border-color:#e46a6a}'
    '.nightMode .qb-ok .qb-l,.night_mode .qb-ok .qb-l{background:#3fbf85;color:#0e211b}'
    '.nightMode .qb-bad .qb-l,.night_mode .qb-bad .qb-l{background:#e46a6a;color:#2a1112}'
)

# One line, ES5, no "</" inside. __ID__ and __ANS__ are filled with JSON strings.
TAP_JS = (
    '(function(){var id=__ID__,ans=__ANS__,K="qb_pick";function run(){'
    'var box=document.querySelector(\'.qb-opts[data-id="\'+id+\'"]\');if(!box)return;'
    'var opts=box.querySelectorAll(".qb-opt"),back=document.querySelector(\'.qb-ans[data-id="\'+id+\'"]\');'
    'function put(v){window.__qbPick=v;try{sessionStorage.setItem(K,v)}catch(e){}try{localStorage.setItem(K,v)}catch(e){}}'
    'function mine(v){return v&&v.indexOf(id+"|")===0?v.slice(id.length+1):null}'
    'function get(){var v=mine(window.__qbPick);if(v===null){try{v=mine(sessionStorage.getItem(K))}catch(e){}}'
    'if(v===null){try{v=mine(localStorage.getItem(K))}catch(e){}}return v}'
    'function paint(pick){for(var i=0;i<opts.length;i++){var l=opts[i].getAttribute("data-l");'
    'if(l===ans)opts[i].className+=" qb-ok";else if(pick!==null&&l===pick)opts[i].className+=" qb-bad"}'
    'box.className=box.className.replace(" qb-live","")+" qb-done"}'
    'function flip(){try{if(typeof pycmd==="function"){pycmd("ans");return}}catch(e){}'
    'try{if(typeof window.showAnswer==="function"){window.showAnswer();return}}catch(e){}'
    'try{if(typeof window.sendMessage2==="function"){window.sendMessage2("ankitap","midCenter")}}catch(e){}}'
    'if(back){paint(get());back.style.display="none";return}'
    'put("");box.className+=" qb-live";'
    'for(var i=0;i<opts.length;i++){opts[i].onclick=function(){if(box.getAttribute("data-done"))return;'
    'box.setAttribute("data-done","1");var l=this.getAttribute("data-l");put(id+"|"+l);paint(l);setTimeout(flip,150)}}}'
    # the back's answer box comes after this script: wait until the page is complete (AnkiDroid loads each side as a page)
    'if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",run);else run()'
    '})();'
)


def _js(v: str) -> str:
    return json.dumps(v, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


def esc(t: str | None) -> str:
    return html.escape(tidy(t or ""), quote=False).replace("\r", "").replace("\t", " ").replace("\n", "<br>")


def option_text(q: dict, label: str | None) -> str:
    for o in q["options"]:
        if label and o["label"] == label:
            return o["text"]
    return ""


def shown_label(q: dict, label: str | None) -> str:
    """The label as the source prints it (A, B, ... or ক, খ, ...)."""
    for o in q["options"]:
        if label and o["label"] == label:
            return o.get("shown") or o["label"]
    return label or ""


def render_front(q: dict, fig_name: str | None, tap: bool = False) -> str:
    attr = (lambda o: f' data-l="{html.escape(o["label"])}"') if tap else (lambda o: "")
    opts = "".join(f'<div class="qb-opt"{attr(o)}><span class="qb-l">{esc(o.get("shown") or o["label"])}</span>'
                   f'<span class="qb-t">{esc(o["text"])}</span></div>' for o in q["options"])
    fig = f'<div class="qb-fig"><img src="{fig_name}"></div>' if fig_name else ""
    chip = f'<div class="qb-chip">{esc(q["subject"])}</div>' if q.get("subject") else ""
    if not tap:
        return (f'<style>{CSS}</style><div class="qb">{chip}<div class="qb-q">{esc(q["stem"])}</div>{fig}'
                f'<div class="qb-opts">{opts}</div></div>')
    script = TAP_JS.replace("__ID__", _js(q["guid"])).replace("__ANS__", _js(q["correct_label"] or ""))
    return (f'<style>{CSS}{TAP_CSS}</style><div class="qb">{chip}<div class="qb-q">{esc(q["stem"])}</div>{fig}'
            f'<div class="qb-opts" data-id="{html.escape(q["guid"])}">{opts}</div></div><script>{script}</script>')


def render_back(q: dict, source_line: str | None, tap: bool = False) -> str:
    lab = q["correct_label"]
    did = f' data-id="{html.escape(q["guid"])}"' if tap else ""
    parts = ['<div class="qb">',
             f'<div class="qb-ans"{did}><span class="qb-l">{esc(shown_label(q, lab))}</span>'
             f'<span class="qb-t">{esc(option_text(q, lab))}</span></div>']
    fact = q.get("fact") or {}
    if q.get("answer_source") == "ai":
        parts.append('<div class="qb-note">Answer worked out by AI (the source did not give it)</div>')
    elif q.get("answer_source") == "ai_corrected":
        parts.append(f'<div class="qb-note">Answer corrected by AI; the source said {esc(fact.get("source_answer_shown") or "")}</div>')
    if fact.get("outdated") and fact.get("outdated_note"):
        parts.append(f'<div class="qb-note"><b>Update (AI):</b> {esc(fact["outdated_note"])}</div>')
    if q.get("explanation"):
        parts.append(f'<div class="qb-exp">{esc(q["explanation"])}</div>')
    elif q.get("ai_explanation"):
        parts.append(f'<div class="qb-exp">{esc(q["ai_explanation"])}</div><div class="qb-note">AI-generated explanation (not from the source)</div>')
    if source_line:
        parts.append(f'<div class="qb-src">{esc(source_line)}</div>')
    parts.append("</div>")
    return "".join(parts)


def source_line_for(cfg, db, q: dict, apps: list[dict]) -> str | None:
    mode = str(cfg.get("anki", "source_line", "exam")).lower()
    if mode == "none":
        return None
    exam_labels: list[str] = []
    for a in apps:
        if a["exam_id"] is not None:
            lab = db.exam_display(a["exam_id"])
            if lab not in exam_labels:
                exam_labels.append(lab)
    exam_part = db.exam_display(q["exam_id"]) if q["exam_id"] is not None else ""
    if mode == "full":
        line = " · ".join(x for x in (exam_part, f"p.{q.get('printed_page')}", f"Q{q['number']}") if x and x != "p.None")
    else:
        line = exam_part or None
    if line and len(apps) > 1:
        others = [l for l in exam_labels if l != exam_part]
        if others:
            line += " · also asked in: " + "; ".join(others)
    return line


def preview(cfg, db, qid: int) -> dict | None:
    """The card exactly as it will be exported (the figure is served from the work folder), for the app's preview."""
    q = next(iter(db.list_questions("q.id=?", (qid,))), None)
    if not q or not q.get("correct_label"):
        return None
    fig = None
    if q.get("figure_path"):
        try:
            fig = "/file/" + quote(str((cfg.root / q["figure_path"]).resolve().relative_to(cfg.work.resolve())).replace("\\", "/"), safe="/")
        except ValueError:
            fig = None
    return {"front": render_front(q, fig), "back": render_back(q, source_line_for(cfg, db, q, db.appearances(qid)))}


def find_anki_media_dirs() -> list[str]:
    """Anki's collection.media folders on this computer (Windows, macOS, Linux)."""
    bases = []
    if os.environ.get("APPDATA"):
        bases.append(Path(os.environ["APPDATA"]) / "Anki2")
    home = Path.home()
    bases += [home / "Library" / "Application Support" / "Anki2", home / ".local" / "share" / "Anki2"]
    found = []
    for b in bases:
        if b.is_dir():
            for prof in sorted(b.iterdir()):
                m = prof / "collection.media"
                if m.is_dir():
                    found.append(str(m))
    return found


def export(cfg, db, include_all: bool = False, anki_media: str | None = None) -> dict:
    where = "q.status IN ('ok','approved')"
    if not include_all:
        where += " AND q.exported_at IS NULL"
    qs = db.list_questions(where)
    deck_root = cfg.get("anki", "deck_root", "Job Questions")
    notetype = cfg.get("anki", "notetype", "Basic")
    page_tag = bool(cfg.get("anki", "page_tag", False))
    tap = bool(cfg.get("anki", "tap_to_answer", True))

    out_dir = cfg.out_dir
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    anki_media_dir = Path(anki_media) if anki_media else None
    if anki_media_dir and not anki_media_dir.is_dir():
        raise FileNotFoundError(f"Anki media folder not found: {anki_media_dir}")

    rows, skipped, exported_ids = [], [], []
    for q in qs:
        if not q["correct_label"] or not q["stem"]:
            skipped.append((q["id"], "no correct answer or empty question"))
            continue
        fig_name = None
        if q.get("figure_path"):
            src = cfg.root / q["figure_path"]
            if not src.exists():
                skipped.append((q["id"], f"figure file missing: {q['figure_path']}"))
                continue
            fig_name = f"qb_{q['guid']}.jpg"
            shutil.copyfile(src, media_dir / fig_name)
            if anki_media_dir:
                shutil.copyfile(src, anki_media_dir / fig_name)
        apps = db.appearances(q["id"])
        exam_labels = []
        for a in apps:
            if a["exam_id"] is not None and db.exam_display(a["exam_id"]) not in exam_labels:
                exam_labels.append(db.exam_display(a["exam_id"]))
        tags = [f"Subject::{slug_tag(q['subject'])}"] + [f"Exam::{slug_tag(l)}" for l in exam_labels]
        if q.get("answer_source") == "ai":
            tags.append("Answer::AI")
        elif q.get("answer_source") == "ai_corrected":
            tags.append("Answer::AI_corrected")
        if (q.get("fact") or {}).get("outdated"):
            tags.append("Check::outdated")
        if page_tag and q.get("printed_page"):
            tags.append(f"page_{q['printed_page']}")
        rows.append([q["guid"], notetype, f"{deck_root}::{q['subject']}", render_front(q, fig_name, tap),
                     render_back(q, source_line_for(cfg, db, q, apps), tap), " ".join(tags)])
        exported_ids.append(q["id"])

    path = None
    if rows:
        path = out_dir / f"anki_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(path, "w", encoding="utf-8", newline="") as f:
            for line in HEADER:
                f.write(line + "\n")
            csv.writer(f, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
        stamp = now()
        for qid in exported_ids:
            db.update_question(qid, exported_at=stamp)
    return {"path": path, "count": len(rows), "skipped": skipped}

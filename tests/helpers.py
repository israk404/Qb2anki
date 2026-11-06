"""Shared test helpers: a fake Gemini, a synthetic photo, sample pages."""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from qb2anki.config import DEFAULTS, Config, _merge  # noqa: E402

LAYOUT = {"text_upright": True, "pages": [{"printed_page": 568, "page_box": [5, 4, 960, 498]},
                                          {"printed_page": 569, "page_box": [5, 504, 960, 996]}]}


def make_cfg(root: Path, **checks) -> Config:
    base = {"ai_fill": False, "math_check": False, "cross_check": False, "proofread": False,
            "fact_check": False}                                                          # tests switch features on when needed
    cfg = Config(root, _merge(DEFAULTS, {"checks": {**base, **checks}}))
    cfg.ensure_dirs()
    return cfg


def tmpdir(test) -> Path:
    d = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, d, True)
    return d


def photo(path: Path, size=(1200, 900)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return path


def q(number, stem, opts=("a", "b", "c", "d"), expl="", labels="কখগঘঙ", **kw):
    d = {"number": str(number), "stem": stem, "options": [{"label": l, "text": t} for l, t in zip(labels, opts)],
         "explanation": expl, "subject": kw.pop("subject", "English"), "has_figure": False}
    d.update(kw)
    return d


def page(questions, key=None, header=None):
    return {"questions": questions, "answer_key": [{"number": str(n), "answer": a} for n, a in (key or [])], "exam_header": header}


def _items(prompt: str):
    return json.loads(prompt.split("Items (JSON):\n", 1)[1])


class FakeClient:
    """Stands in for Gemini; answers depend on the call label. Records every call."""
    api_calls = 0
    cache_hits = 0
    pool = None

    def __init__(self, layout=None, pages=None, pages_b=None, math_fn=None, reread_text="", fill_answer="খ", fill_conf="high",
                 detect=None, proof_fn=None, check_fn=None):
        self.layout, self.pages, self.pages_b = layout or LAYOUT, pages or {}, pages_b or {}
        self.detect = detect or {"kind": "spread", "rotate_ccw_degrees": "90"}
        self.proof_fn = proof_fn
        self.check_fn = check_fn
        self.math_fn, self.reread_text = math_fn, reread_text
        self.fill_answer, self.fill_conf = fill_answer, fill_conf
        self.calls, self.usage_log = [], []

    def warm_up(self):
        pass

    def list_models(self):
        return ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.6-flash-tts"]

    def check_key(self, secret, model):
        return ("ok", "") if secret.endswith("x" * 30) else ("bad", "invalid")

    def generate_json(self, *, model, prompt, schema, image=None, system=None, label="", allow_fallback=True):
        self.calls.append(label)
        self.api_calls += 1
        if label in ("doctor",) or label.startswith("test "):
            return {"ok": "pro" not in model}
        if label == "layout":
            return self.layout
        if label == "detect":
            return self.detect
        if label.startswith("check"):
            default = lambda it: {"id": it["id"], "text_ok": True, "answer": it.get("source_answer") or self.fill_answer,  # noqa: E731
                                  "answer_confidence": self.fill_conf, "agrees": "yes" if it.get("source_answer") else "no_source",
                                  "outdated": False, "explanation_ok": True,
                                  "explanation": f"AI note {it['id']}" if it["need_explanation"] else ""}
            return {"results": [(self.check_fn or default)(it) for it in _items(prompt)]}
        if label.startswith("proof"):
            return {"results": [r for r in (self.proof_fn(it) for it in _items(prompt)) if r] if self.proof_fn else []}
        if label.startswith("page B "):
            return self.pages_b[label.split()[2]]
        if label.startswith("page "):
            return self.pages[label.split()[1]]
        if label.startswith("math"):
            default = lambda it: {"id": it["id"], "consistent": True, "fixed_explanation": "", "note": ""}  # noqa: E731
            return {"results": [(self.math_fn or default)(it) for it in _items(prompt)]}
        if label.startswith("reread"):
            return {"explanation": self.reread_text}
        if label.startswith("fill"):
            return {"results": [{"id": it["id"], "answer": self.fill_answer if it["need_answer"] else None,
                                 "confidence": self.fill_conf, "explanation": f"AI note {it['id']}"} for it in _items(prompt)]}
        raise AssertionError(f"unexpected call {label}")

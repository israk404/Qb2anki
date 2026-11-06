"""The full AI cross-check (text only) and the one-key / rotation switch."""
from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace

from helpers import DEFAULTS, FakeClient, make_cfg, page, photo, q, tmpdir
from qb2anki import assemble, export_anki, factcheck
from qb2anki.db import DB
from qb2anki.pipeline import Pipeline
from qb2anki.schemas import CheckModel, PageModel


def card(stem="বাংলাদেশের রাষ্ট্রপতি কে?", opts=("ক ব্যক্তি", "খ ব্যক্তি", "গ ব্যক্তি", "ঘ ব্যক্তি"), correct="খ", expl="",
         subject="General Knowledge"):
    c = assemble.normalize_question(PageModel.model_validate({"questions": [q(1, stem, opts, expl, subject=subject)]}).questions[0],
                                    DEFAULTS["subjects"])
    c.update(correct_label=correct, flags=[], answer_source="key" if correct else None)
    if not correct:
        c["flags"].append({"code": "missing_answer", "msg": "x", "hard": True})
    return c


def verdict(**kw):
    base = {"id": "0", "text_ok": True, "answer": None, "answer_confidence": "high", "agrees": "yes", "outdated": False,
            "explanation_ok": True}
    base.update(kw)
    return CheckModel.model_validate({"results": [base]})


def codes(c):
    return {f["code"]: f["hard"] for f in c["flags"]}


class Policies(unittest.TestCase):
    def test_agreement_leaves_the_card_alone(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="খ"))
        self.assertEqual((c["correct_label"], codes(c)), ("খ", {}))
        self.assertEqual(c["fact"]["answer"], "খ")

    def test_disagreement_review_holds_the_card(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="গ", agrees="no", note="changed in 2023"), "review")
        self.assertEqual(c["correct_label"], "খ")                                   # the source answer is kept
        self.assertEqual(codes(c), {"answer_disputed": True})
        self.assertIn("গ", c["flags"][0]["msg"])

    def test_disagreement_auto_corrects_only_when_sure(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="গ", agrees="no"), "auto")
        self.assertEqual((c["correct_label"], c["answer_source"]), ("গ", "ai_corrected"))
        self.assertEqual(codes(c), {"answer_corrected": False})
        self.assertEqual(c["fact"]["source_answer"], "খ")
        c2 = card()
        factcheck.apply_job({"0": c2}, verdict(answer="গ", agrees="no", answer_confidence="medium"), "auto")
        self.assertEqual((c2["correct_label"], codes(c2)), ("খ", {"answer_disputed": True}))

    def test_disagreement_note_only(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="গ", agrees="no"), "note")
        self.assertEqual((c["correct_label"], codes(c)), ("খ", {"answer_disputed": False}))

    def test_missing_answer_is_filled_with_confidence_rules(self):
        c = card(correct=None)
        factcheck.apply_job({"0": c}, verdict(answer="ঘ", agrees="no_source"))
        self.assertEqual((c["correct_label"], c["answer_source"], codes(c)), ("ঘ", "ai", {"ai_answer": False}))
        c2 = card(correct=None)
        factcheck.apply_job({"0": c2}, verdict(answer="ঘ", agrees="no_source", answer_confidence="low"))
        self.assertEqual(codes(c2), {"ai_answer": True})

    def test_outdated_fact(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="খ", outdated=True, outdated_note="Since 2023 the office is held by someone else."))
        self.assertEqual(codes(c), {"outdated": True})
        self.assertTrue(c["fact"]["outdated"])
        c2 = card()
        factcheck.apply_job({"0": c2}, verdict(answer="গ", agrees="no", outdated=True, outdated_note="now গ"), "auto")
        self.assertEqual((c2["correct_label"], codes(c2)), ("গ", {"answer_corrected": False, "outdated": False}))

    def test_explanations(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="খ", explanation="সংক্ষিপ্ত ব্যাখ্যা।"))
        self.assertEqual(c["ai_explanation"], "সংক্ষিপ্ত ব্যাখ্যা।")               # missing explanation written
        c2 = card(expl="ভুল ব্যাখ্যা")
        factcheck.apply_job({"0": c2}, verdict(answer="খ", explanation_ok=False, explanation_issue="names the wrong person",
                                               explanation="সঠিক ব্যাখ্যা"), "review")
        self.assertEqual((c2["explanation"], c2["ai_explanation"], codes(c2)), ("ভুল ব্যাখ্যা", "সঠিক ব্যাখ্যা", {"explanation_disputed": True}))
        c3 = card(expl="ভুল ব্যাখ্যা")
        factcheck.apply_job({"0": c3}, verdict(answer="খ", explanation_ok=False, explanation_issue="wrong", explanation="সঠিক ব্যাখ্যা"), "auto")
        self.assertEqual((c3["explanation"], c3["explanation_original"], codes(c3)), ("সঠিক ব্যাখ্যা", "ভুল ব্যাখ্যা", {"explanation_corrected": False}))

    def test_damaged_text_is_repaired_or_flagged(self):
        c = card(stem="বাংলাদেশের রাষট্রপতি কে্?", opts=("ক ব্যক্তি", "খ ব্যক্তি", "গ ব্যক্তি", "ঘ ব্য[?]"))
        factcheck.apply_job({"0": c}, verdict(answer="খ", text_ok=False, fixed_stem="বাংলাদেশের রাষ্ট্রপতি কে?",
                                              fixed_options=[{"label": l, "text": t} for l, t in zip("কখগঘ", ["ক ব্যক্তি", "খ ব্যক্তি", "গ ব্যক্তি", "ঘ ব্যক্তি"])]))
        self.assertEqual((c["stem"], c["options"][3]["text"]), ("বাংলাদেশের রাষ্ট্রপতি কে?", "ঘ ব্যক্তি"))
        self.assertEqual(c["proof"]["stem"], "বাংলাদেশের রাষট্রপতি কে্?")         # the first reading is kept
        self.assertEqual(codes(c), {"fact_text_fixed": False})                     # the [?] flag is gone too
        c2 = card()
        factcheck.apply_job({"0": c2}, verdict(answer="খ", text_ok=False, fixed_stem="a completely different question about rivers"))
        self.assertEqual(c2["stem"], "বাংলাদেশের রাষ্ট্রপতি কে?")                     # a different question is refused
        self.assertEqual(codes(c2), {"text_suspect": True})

    def test_a_recheck_replaces_the_previous_verdict(self):
        c = card()
        factcheck.apply_job({"0": c}, verdict(answer="গ", agrees="no"))
        factcheck.apply_job({"0": c}, verdict(answer="খ"))
        self.assertEqual(codes(c), {})


class InThePipeline(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self), fact_check=True, ai_fill=True)
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)
        photo(self.cfg.inbox / "screenshot" / "s.png", (900, 600))

    def test_every_question_is_checked_in_batches_and_replaces_the_fill_step(self):
        qs = [q(n, f"Question {n}?", expl="x", answer_in_explanation="ক") for n in range(1, 11)] + [q(11, "No answer?")]
        fake = FakeClient(pages={"p0": page(qs)}, fill_answer="গ")
        Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        self.assertEqual([c for c in fake.calls if c.startswith(("check", "fill"))], ["check 0", "check 1"])   # 11 questions, batches of 8
        cards = {c["number"]: c for c in self.db.list_questions()}
        self.assertTrue(all(c["fact"] for c in cards.values()))
        self.assertEqual((cards[11]["correct_label"], cards[11]["answer_source"]), ("গ", "ai"))
        self.assertTrue(cards[11]["ai_explanation"])

    def test_a_disputed_answer_waits_and_shows_on_the_card_when_corrected(self):
        def disagree(it):
            return {"id": it["id"], "text_ok": True, "answer": "গ", "answer_confidence": "high", "agrees": "no", "outdated": True,
                    "outdated_note": "changed in 2024", "explanation_ok": True}
        fake = FakeClient(pages={"p0": page([q(1, "Who?", expl="x", answer_in_explanation="খ")])}, check_fn=disagree)
        Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        c = self.db.list_questions()[0]
        self.assertEqual((c["status"], c["correct_label"]), ("review", "খ"))
        self.cfg.raw["checks"]["fact_policy"] = "auto"
        from qb2anki.factcheck import check_database
        r = check_database(self.cfg, self.db, FakeClient(check_fn=disagree), "m", recheck=True)
        self.assertEqual(r["checked"], 1)
        c = self.db.list_questions()[0]
        self.assertEqual((c["status"], c["correct_label"], c["answer_source"]), ("ok", "গ", "ai_corrected"))
        text = export_anki.export(self.cfg, self.db)["path"].read_text(encoding="utf-8")
        self.assertIn("Answer corrected by AI; the source said খ", text)
        self.assertIn("Update (AI):", text)
        self.assertIn("Answer::AI_corrected", text)
        self.assertIn("Check::outdated", text)


class StoredCards(unittest.TestCase):
    def test_only_unchecked_cards_are_checked_unless_rechecking_and_approved_cards_are_untouched(self):
        cfg = make_cfg(tmpdir(self))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        photo(cfg.inbox / "screenshot" / "s.png", (900, 600))
        Pipeline(cfg, db, FakeClient(pages={"p0": page([q(n, f"Q{n}?", expl="x", answer_in_explanation="ক") for n in (1, 2, 3)])}),
                 say=lambda *_: None).run()
        export_anki.export(cfg, db)
        ids = {c["number"]: c["id"] for c in db.list_questions()}
        db.update_question(ids[3], status="approved")
        from qb2anki.factcheck import check_database
        fake = FakeClient()
        self.assertEqual(check_database(cfg, db, fake, "m")["checked"], 2)
        self.assertEqual(check_database(cfg, db, fake, "m")["checked"], 0)                 # already checked: no requests
        self.assertEqual(check_database(cfg, db, fake, "m", recheck=True)["checked"], 2)
        self.assertIsNone(db.get_question(ids[3])["fact"])
        self.assertIsNotNone(db.get_question(ids[1])["exported_at"])                       # unchanged: not re-exported

        def wrong(it):
            return {"id": it["id"], "text_ok": True, "answer": "ঘ", "answer_confidence": "high", "agrees": "no", "outdated": False,
                    "explanation_ok": True}
        cfg.raw["checks"]["fact_policy"] = "auto"
        check_database(cfg, db, FakeClient(check_fn=wrong), "m", recheck=True)
        self.assertIsNone(db.get_question(ids[1])["exported_at"])                          # corrected: exportable again


class KeySwitch(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self))
        self.cfg.raw["gemini"].update({"request_delay_sec": 0, "requests_per_minute": 0, "fallback_models": [], "key_daily_cap": 50})
        (self.cfg.root / "keys.txt").write_text("\n".join("AIza" + f"{i:02d}" + "x" * 30 for i in range(10)), encoding="utf-8")
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)
        os.environ.pop("GEMINI_API_KEY", None)

    def client(self):
        from qb2anki.gemini_client import GeminiClient
        c = GeminiClient(self.cfg, use_cache=False, db=self.db)
        used, lock = [], threading.Lock()

        def factory(key):
            class Models:
                def generate_content(s, model, contents, config):
                    with lock:
                        used.append(key.label)
                    return SimpleNamespace(text='{"ok": true}', usage_metadata=None)
            return SimpleNamespace(models=Models())
        c._client_factory = factory
        return c, used

    def test_one_key_mode_ignores_the_key_list(self):
        self.cfg.raw["gemini"]["key_mode"] = "single"
        c, _ = self.client()
        self.assertIsNone(c.pool)

    def test_rotation_spreads_requests_evenly_even_in_parallel(self):
        self.cfg.raw["gemini"]["key_mode"] = "rotate"
        c, used = self.client()
        self.assertEqual(len(c.pool.keys), 10)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda i: c.generate_json(model="m", prompt=f"p{i}", schema={"type": "OBJECT"}, label="t"), range(40)))
        counts = {k: used.count(k) for k in set(used)}
        self.assertEqual(len(counts), 10)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)              # 40 requests over 10 keys: 4 each


if __name__ == "__main__":
    unittest.main()

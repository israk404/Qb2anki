"""End-to-end pipeline behaviour with a fake Gemini."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import LAYOUT, FakeClient, make_cfg, page, photo, q, tmpdir
from qb2anki.db import DB
from qb2anki.errors import QuotaExhausted
from qb2anki.pipeline import Pipeline
from qb2anki.review import Review


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.db = None

    def setup(self, **checks):
        self.cfg = make_cfg(self.root, **checks)
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)

    def run_pipe(self, client, **kw):
        return Pipeline(self.cfg, self.db, client, say=lambda *_: None).run(**kw)

    def cards(self):
        return {c["number"]: c for c in self.db.list_questions()}

    def codes(self, c):
        return {f["code"] for f in c["flags"]}


class BookPhotos(Base):
    def test_a_spread_costs_three_requests_and_stores_everything(self):
        self.setup()
        photo(self.cfg.inbox / "spread1.jpg")
        pages = {"p0": page([q("৩৭", "Puerperal কত দিন?", ["১৫", "২৪", "৪২", "৫০"], "ব্যাখ্যা", subject="Nursing & Midwifery"),
                             q("৩৮", "Second?", has_figure=True, figure_box=[10, 10, 40, 90], question_box=[0, 0, 50, 1000])],
                            [("৩৭", "গ"), ("৩৮", "খ")], {"title": "Exam Alpha 2024", "exam_date": "2024"}),
                 "p1": page([q("৫০", "Later?", expl="x")], [("৫০", "ক")])}
        fake = FakeClient(pages=pages)
        rep = self.run_pipe(fake)
        self.assertEqual(fake.calls, ["detect", "layout", "page p0", "page p1"])    # auto: 1 small request finds the photo type
        self.assertEqual((rep["failed"], rep["inserted"]), ([], 3))
        wd = self.cfg.resolve(self.db.photo_by_stem("spread1")["workdir"])
        for name in ("spread.jpg", "layout_overlay.jpg", "crops/p0_page.jpg", "crops/p1_page.jpg"):
            self.assertTrue((wd / name).exists(), name)
        c = self.cards()
        self.assertEqual(self.db.exam_display(c[37]["exam_id"]), "Exam Alpha 2024")       # header of page 568 ...
        self.assertEqual(self.db.exam_display(c[50]["exam_id"]), "Exam Alpha 2024")       # ... is inherited by page 569
        self.assertEqual((c[37]["correct_label"], c[37]["status"]), ("গ", "ok"))
        self.assertEqual(c[38]["status"], "review")                                      # a figure must be looked at
        self.assertTrue((self.root / c[38]["figure_path"]).exists())
        d = Review(self.cfg, self.db).detail(c[37]["id"])
        self.assertTrue(d["context_url"] and d["crop_url"] and d["overlay_url"])
        # reading the same photo again does nothing and costs nothing
        fake2 = FakeClient(pages=pages)
        self.assertEqual(self.run_pipe(fake2)["skipped"], 1)
        self.assertEqual(fake2.calls, [])

    def test_a_missed_question_flags_only_its_neighbours(self):
        self.setup()
        photo(self.cfg.inbox / "a.jpg")
        qs = [q(n, f"Question {n}?", expl="x") for n in (1, 2, 3, 5, 6)]
        self.run_pipe(FakeClient(pages={"p0": page(qs, [(n, "ক") for n in range(1, 7)]), "p1": page([])}))
        self.assertEqual({n: c["status"] for n, c in self.cards().items()}, {1: "ok", 2: "ok", 3: "review", 5: "review", 6: "ok"})

    def test_bad_layout_is_rejected_and_quota_stop_keeps_progress(self):
        self.setup()
        photo(self.cfg.inbox / "a.jpg")
        bad = {"text_upright": True, "pages": [{"page_box": [0, 0, 100, 100]}]}
        rep = self.run_pipe(FakeClient(layout=bad))
        self.assertEqual(len(rep["failed"]), 1)
        self.assertEqual(self.db.photo_by_stem("a")["status"], "failed")

        class Quota(FakeClient):
            def generate_json(s, **kw):
                if kw["label"].startswith("page"):
                    raise QuotaExhausted("daily quota used up")
                return super().generate_json(**kw)
        photo(self.cfg.inbox / "b.jpg", (1300, 900))
        rep = self.run_pipe(Quota())
        self.assertIsNotNone(rep["quota_stop"])
        self.assertEqual(rep["photos"], 0)

    def test_stopping_between_photos(self):
        self.setup()
        photo(self.cfg.inbox / "a.jpg")
        photo(self.cfg.inbox / "b.jpg", (1300, 900))
        stop = []

        class Stopper(FakeClient):
            def generate_json(s, **kw):
                r = super().generate_json(**kw)
                if kw["label"] == "page p1":
                    stop.append(1)
                return r
        fake = Stopper(pages={"p0": page([q(1, "A?", expl="x")], [(1, "ক")]), "p1": page([])})
        rep = Pipeline(self.cfg, self.db, fake, say=lambda *_: None, should_stop=lambda: bool(stop)).run()
        self.assertTrue(rep["stopped"])
        self.assertEqual(rep["photos"], 1)

    def test_cross_check_reads_twice_and_flags_differences(self):
        self.setup(cross_check=True)
        photo(self.cfg.inbox / "a.jpg")
        a = page([q(43, "Exclusive Breast Feeding কত দিন?", expl="x")], [(43, "খ")])
        b = page([q(43, "Exclusive Breastfeeding কয় দিন?", expl="x")], [(43, "খ")])
        fake = FakeClient(pages={"p0": a, "p1": page([])}, pages_b={"p0": b, "p1": page([])})
        self.run_pipe(fake)
        self.assertIn("page B p0", fake.calls)
        c = self.cards()[43]
        self.assertIn("extract_disagree_stem", self.codes(c))
        self.assertIn("Breastfeeding", c["alt"]["stem"])


class Screenshots(Base):
    def shot(self, name="quiz.png", folder="screenshot/BCS 45 Preliminary"):
        photo(self.cfg.inbox / folder / name, (900, 600))

    def test_profiles_and_folder_names(self):
        self.setup()
        pipe = Pipeline(self.cfg, self.db, FakeClient(), say=lambda *_: None)
        i = self.cfg.inbox
        self.assertEqual(pipe.resolve_profile(i / "a.jpg")[0::2], ("auto", None))
        self.assertEqual(pipe.resolve_profile(i / "Screenshot" / "BCS 45" / "b.png")[0::2], ("screenshot", "BCS 45"))
        self.assertEqual(pipe.resolve_profile(i / "My Exam" / "c.png")[0::2], ("auto", "My Exam"))
        self.assertEqual(pipe.resolve_profile(i / "book" / "d.jpg")[0::2], ("book", None))

    def test_web_quiz_without_answers_one_read_plus_ai_fill_and_labels(self):
        self.setup(ai_fill=True)
        self.shot()
        fake = FakeClient(pages={"p0": page([q("১", "কোন দেশ?", ["মিশর", "তিউনিশিয়া", "লিবিয়া", "সিরিয়া"], subject="General Knowledge")])}, fill_answer="খ")
        self.run_pipe(fake)
        self.assertEqual(fake.calls, ["page p0", "fill 0"])                        # no layout request
        c = self.cards()[1]
        self.assertEqual((c["correct_label"], c["answer_source"], c["status"]), ("খ", "ai", "ok"))
        self.assertTrue(c["ai_explanation"].startswith("AI note"))
        self.assertEqual(self.db.exam_display(c["exam_id"]), "BCS 45 Preliminary")
        self.assertIn("ai_answer", self.codes(c))

    def test_unsure_ai_answer_must_be_approved(self):
        self.setup(ai_fill=True)
        self.shot()
        self.run_pipe(FakeClient(pages={"p0": page([q(1, "Hard?")])}, fill_conf="low"))
        c = self.cards()[1]
        self.assertEqual(c["status"], "review")
        self.assertTrue(any(f["code"] == "ai_answer" and f["hard"] for f in c["flags"]))

    def test_inline_answers_need_no_ai_answer_only_an_explanation(self):
        self.setup(ai_fill=True)
        self.shot()
        fake = FakeClient(pages={"p0": page([q(72, "রাষ্ট্রভাষা কমিটি?", ["১৯৪৮", "১৯৫০", "১৯৫২", "১৯৫৪"], answer_in_explanation="ক")])})
        self.run_pipe(fake)
        c = self.cards()[72]
        self.assertEqual((c["correct_label"], c["answer_source"]), ("ক", "printed"))
        self.assertTrue(c["ai_explanation"])
        self.assertNotIn("ai_answer", self.codes(c))

    def test_answer_line_with_latin_labels_and_a_real_title_beats_the_folder(self):
        self.setup()
        self.shot()
        mk = lambda n, s: q(n, s, ["w", "x", "y", "z"], "x", labels="ABCD", subject="General Knowledge")  # noqa: E731
        self.run_pipe(FakeClient(pages={"p0": page([mk("৩১", "প্রথম?"), mk("৩২", "দ্বিতীয়?")], [("৩১", "A"), ("৩২", "B")],
                                                   {"title": "বিসিএস প্রিলিমিনারি পরীক্ষা"})}))
        c = self.cards()
        self.assertEqual((c[31]["correct_label"], c[32]["correct_label"]), ("ক", "খ"))
        self.assertEqual([o["shown"] for o in c[31]["options"]], ["A", "B", "C", "D"])
        self.assertEqual(self.db.exam_display(c[31]["exam_id"]), "বিসিএস প্রিলিমিনারি পরীক্ষা")


class MathFlow(Base):
    def test_broken_formula_is_repaired_and_the_original_is_kept(self):
        self.setup(math_check=True)
        photo(self.cfg.inbox / "screenshot" / "m.png", (900, 600))
        broken = r"এখানে \( \frac{1}{2 \)"
        fixed = r"এখানে \( \frac{1}{2} \)"
        fake = FakeClient(pages={"p0": page([q(8, "সেট?", ["৩", "৭", "৮", "৯"], broken, answer_in_explanation="খ", subject="Math")])},
                          math_fn=lambda it: {"id": it["id"], "consistent": True, "fixed_explanation": fixed, "note": ""})
        self.run_pipe(fake)
        c = self.cards()[8]
        self.assertEqual((c["explanation"], c["explanation_original"]), (fixed, broken))
        self.assertNotIn("latex", self.codes(c))
        self.assertIn("math_fixed", self.codes(c))
        self.assertEqual(c["status"], "ok")
        self.assertEqual([x for x in fake.calls if x.startswith("reread")], [])         # no picture re-read needed

    def test_still_broken_math_is_read_again_from_the_picture(self):
        self.setup(math_check=True)
        photo(self.cfg.inbox / "screenshot" / "m.png", (900, 600))
        broken = r"\( \frac{a}{b \)"
        good = r"\( \frac{a}{b} \)"
        qq = q(8, "সেট?", ["৩", "৭", "৮", "৯"], broken, answer_in_explanation="খ", subject="Math", question_box=[0, 0, 100, 1000])
        fake = FakeClient(pages={"p0": page([qq])}, reread_text=good)            # the text check repairs nothing
        self.run_pipe(fake)
        self.assertTrue([x for x in fake.calls if x.startswith("reread")])
        c = self.cards()[8]
        self.assertEqual((c["explanation"], c["explanation_original"]), (good, broken))
        self.assertIn("math_reread", self.codes(c))
        self.assertNotIn("latex", self.codes(c))

    def test_working_that_misses_the_answer_waits_for_review(self):
        self.setup(math_check=True)
        photo(self.cfg.inbox / "screenshot" / "m.png", (900, 600))
        fake = FakeClient(pages={"p0": page([q(1, "2+2?", ["3", "4", "5", "6"], r"\( 2 + 2 = 5 \)", answer_in_explanation="খ", subject="Math")])},
                          math_fn=lambda it: {"id": it["id"], "consistent": False, "fixed_explanation": "", "note": "2+2 is 4"})
        self.run_pipe(fake)
        c = self.cards()[1]
        self.assertEqual(c["status"], "review")
        self.assertIn("math_inconsistent", self.codes(c))

    def test_math_check_off_makes_no_math_requests(self):
        self.setup(math_check=False)
        photo(self.cfg.inbox / "screenshot" / "m.png", (900, 600))
        fake = FakeClient(pages={"p0": page([q(1, "2+2?", expl=r"\( 4 \)", answer_in_explanation="খ", subject="Math")])})
        self.run_pipe(fake)
        self.assertEqual(fake.calls, ["page p0"])


class SavedAnswers(Base):
    def test_deleting_the_database_and_reading_again_costs_no_requests(self):
        from qb2anki.gemini_client import GeminiClient
        self.setup()
        self.cfg.raw["gemini"].update({"request_delay_sec": 0, "requests_per_minute": 0})
        photo(self.cfg.inbox / "screenshot" / "a.png", (900, 600))
        pg = json.dumps(page([q(1, "Q?", expl="x", answer_in_explanation="খ")]))

        class Models:
            n = 0

            def generate_content(s, model, contents, config):
                Models.n += 1
                return SimpleNamespace(text=pg, usage_metadata=None)
        c1 = GeminiClient(self.cfg)
        c1._client = SimpleNamespace(models=Models())
        self.run_pipe(c1)
        self.assertEqual(Models.n, 1)
        self.db.close()
        self.cfg.db_path.unlink()
        self.db = DB(self.cfg.db_path)

        class Offline(GeminiClient):
            def _call_once(self, *a, **k):
                raise AssertionError("a request was attempted")
        c2 = Offline(self.cfg)
        rep = self.run_pipe(c2)
        self.assertEqual((rep["inserted"], c2.api_calls, c2.cache_hits), (1, 0, 1))

    def test_pending_lists_only_unread_photos(self):
        self.setup()
        photo(self.cfg.inbox / "screenshot" / "a.png", (900, 600))
        photo(self.cfg.inbox / "screenshot" / "b.png", (950, 600))
        pipe = Pipeline(self.cfg, self.db, FakeClient(pages={"p0": page([q(1, "Q?", expl="x", answer_in_explanation="ক")])}), say=lambda *_: None)
        self.assertEqual(len(pipe.pending()), 2)
        pipe.run([self.cfg.inbox / "screenshot" / "a.png"])
        self.assertEqual([p.name for p in pipe.pending()], ["b.png"])


if __name__ == "__main__":
    unittest.main()

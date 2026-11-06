"""Pure logic: text normalisation, formula lint, assembly and flags, config layers, database."""
from __future__ import annotations

import json
import sqlite3
import unittest

from helpers import DEFAULTS, make_cfg, q, tmpdir  # noqa: F401
from qb2anki import assemble, mathcheck, textnorm
from qb2anki.config import load_config, reset_settings, save_settings
from qb2anki.db import DB, quota_day
from qb2anki.flags import finalize_status
from qb2anki.schemas import KeyEntry, MathModel, PageModel, RereadModel


class TextNorm(unittest.TestCase):
    def test_digits_and_labels(self):
        self.assertEqual(textnorm.parse_number("৩৭."), 37)
        self.assertEqual(textnorm.canon_label("(খ)"), "খ")
        self.assertEqual(textnorm.canon_label("ⓑ"), "খ")
        self.assertEqual(textnorm.canon_label("E"), "ঙ")
        self.assertEqual(textnorm.display_label("(b)", "খ"), "B")
        self.assertEqual(textnorm.canon_key_answer("Note"), "NOTE")
        self.assertIsNone(textnorm.canon_key_answer("?"))

    def test_hash_ignores_option_order_digits_and_punctuation(self):
        a = textnorm.content_hash("Puerperal Period কত দিন?", ["১৫ দিন", "২৪ দিন", "৪২ দিন", "৫০ দিন"])
        b = textnorm.content_hash("puerperal period  কত দিন", ["50 দিন", "42 দিন", "24 দিন", "15 দিন."])
        self.assertEqual(a, b)

    def test_hash_keeps_bengali_vowel_signs_and_math_signs(self):
        self.assertNotEqual(textnorm.norm_text("কি"), textnorm.norm_text("কী"))
        self.assertNotEqual(textnorm.content_hash(r"\(x+y=20\)", ["1"]), textnorm.content_hash(r"\(x-y=20\)", ["1"]))


class MathLint(unittest.TestCase):
    def hard(self, text):
        return mathcheck.hard_problems(text)

    def test_good_formulas_pass(self):
        self.assertEqual(mathcheck.lint(r"\( \frac{a}{b} + \sqrt{2} = \frac12 \)" + "\nএখানে \\(x^2\\) \\( 5 \\text{ টাকা} \\)"), [])
        self.assertEqual(mathcheck.lint(r"\[ \begin{aligned} a &= b \end{aligned} \]"), [])

    def test_typical_breakages_are_caught(self):
        for broken in (r"\( \frac{a}{b + c \)", r"\( \frac{a} \)", r"\( \sqrt \)", r"\( \left( x + 1 \)", r"\( x^ \)", r"\( x + 1"):
            self.assertTrue(self.hard(broken), broken)

    def test_bangla_inside_a_formula_is_a_soft_note(self):
        res = mathcheck.lint(r"\( x = ৫ টাকা \)")
        self.assertEqual([h for h, _ in res], [False])

    def _card(self, expl, subject="Math", key="খ"):
        c = {"stem": "প্রশ্ন", "options": [{"label": l, "shown": l, "text": "t"} for l in "কখগঘ"], "explanation": expl,
             "subject": subject, "correct_label": key, "flags": []}
        return c

    def test_repair_is_accepted_only_if_it_is_a_repair(self):
        good = self._card(r"\( \frac{1}{2 \)")                   # broken brace
        mathcheck.apply_check({"0": good}, MathModel.model_validate({"results": [{"id": "0", "consistent": True, "fixed_explanation": r"\( \frac{1}{2} \)"}]}))
        self.assertEqual(good["explanation"], r"\( \frac{1}{2} \)")
        self.assertEqual(good["explanation_original"], r"\( \frac{1}{2 \)")
        self.assertIn("math_fixed", {f["code"] for f in good["flags"]})
        rewrite = self._card(r"\( 2 + 2 = 4 \)")                  # a total rewrite is refused
        mathcheck.apply_check({"0": rewrite}, MathModel.model_validate({"results": [{"id": "0", "consistent": True, "fixed_explanation": "completely different text about something else"}]}))
        self.assertEqual(rewrite["explanation"], r"\( 2 + 2 = 4 \)")
        self.assertNotIn("explanation_original", rewrite)

    def test_inconsistent_working_is_a_hard_flag(self):
        c = self._card(r"\( 2 + 2 = 5 \)")
        mathcheck.apply_check({"0": c}, MathModel.model_validate({"results": [{"id": "0", "consistent": False, "note": "2+2 is 4"}]}))
        self.assertTrue(any(f["code"] == "math_inconsistent" and f["hard"] for f in c["flags"]))
        self.assertEqual(finalize_status(c), "review")

    def test_reread_replaces_only_when_better(self):
        c = self._card(r"\( \frac{1}{2 \)")
        self.assertTrue(mathcheck.apply_reread(c, RereadModel(explanation=r"\( \frac{1}{2} \)")))
        self.assertEqual(c["explanation_original"], r"\( \frac{1}{2 \)")
        c2 = self._card(r"\( \frac{1}{2} \)")
        self.assertFalse(mathcheck.apply_reread(c2, RereadModel(explanation=r"\( \frac{1}{ \)")))      # worse: refused
        self.assertEqual(c2["explanation"], r"\( \frac{1}{2} \)")

    def test_check_jobs_only_cover_math_with_explanations(self):
        qs = [self._card(r"\( 1 \)"), self._card("", "Math"), self._card("plain text", "English")]
        jobs = mathcheck.build_check_jobs(qs)
        self.assertEqual(sum(len(m) for m, _ in jobs), 1)
        self.assertEqual(jobs[0][1][0]["key"], "খ")


class Assemble(unittest.TestCase):
    checks = DEFAULTS["checks"]

    def run_page(self, a, key=None, b=None, key_b=None):
        pa = PageModel.model_validate({"questions": a, "answer_key": [{"number": str(n), "answer": x} for n, x in (key or [])]})
        pb = PageModel.model_validate({"questions": b, "answer_key": [{"number": str(n), "answer": x} for n, x in (key_b if key_b is not None else (key or []))]}) if b is not None else None
        return assemble.assemble_page(pa, pb, DEFAULTS["subjects"], self.checks)

    def codes(self, qq):
        return {f["code"] for f in qq["flags"]}

    def test_clean_question_has_no_flags(self):
        qs, pf = self.run_page([q("৩৭", "Which?", expl="because b")], [("৩৭", "খ")])
        self.assertEqual(self.codes(qs[0]), set())
        self.assertEqual((qs[0]["correct_label"], qs[0]["answer_source"], pf), ("খ", "key", []))

    def test_cross_check_flags_differences_and_disagreeing_keys(self):
        a = q("1", "S?", ["alpha", "b", "c", "d"])
        b = q("1", "S?", ["alphx", "b", "c", "d"])
        qs, _ = self.run_page([a], [("1", "ক")], b=[b], key_b=[("1", "ক")])
        self.assertIn("extract_disagree_option", self.codes(qs[0]))
        self.assertEqual(qs[0]["alt"]["options"][0]["text"], "alphx")
        qs, _ = self.run_page([q("1", "S?")], [("1", "খ")], b=[q("1", "S?")], key_b=[("1", "ঘ")])
        self.assertIn("key_disagree", self.codes(qs[0]))
        self.assertIsNone(qs[0]["correct_label"])

    def test_missing_question_flags_only_its_neighbours(self):
        qs, pf = self.run_page([q(n, f"Q{n}?") for n in (1, 2, 3, 5, 6)], [(n, "ক") for n in range(1, 7)])
        flag = next(f for f in pf if f["code"] == "question_missing")
        self.assertEqual(flag["near"], [3, 5])

    def test_note_answer_and_key_vs_explanation(self):
        qs, _ = self.run_page([q("5", "Handy", expl="Option (খ) সঠিক", answer_in_explanation="খ")], [("5", "Note")])
        self.assertEqual((qs[0]["correct_label"], self.codes(qs[0])), ("খ", {"answer_from_note"}))
        qs, _ = self.run_page([q("5", "S", expl="(গ)", answer_in_explanation="গ")], [("5", "খ")])
        self.assertIn("key_vs_explanation", self.codes(qs[0]))
        self.assertEqual(finalize_status(qs[0]), "review")

    def test_printed_answers_without_a_key_and_missing_answers(self):
        qs, _ = self.run_page([q("72", "S", answer_in_explanation="ক")])
        self.assertEqual((qs[0]["correct_label"], qs[0]["answer_source"]), ("ক", "printed"))
        self.assertEqual(finalize_status(qs[0]), "ok")
        qs, _ = self.run_page([q("1", "S")])
        self.assertIn("missing_answer", self.codes(qs[0]))

    def test_option_counts_and_labels(self):
        five = q("30", "S", ["w", "x", "y", "z", "none"])
        qs, _ = self.run_page([five], [("৩০", "ঙ")])
        self.assertEqual((self.codes(qs[0]), qs[0]["correct_label"]), (set(), "ঙ"))
        six = q("1", "S", ["a"] * 6, labels="কখগঘঙচ")
        qs, _ = self.run_page([six], [("1", "ক")])
        self.assertIn("option_count", self.codes(qs[0]))

    def test_latin_labels_are_kept_for_display(self):
        d = q("৩১", "প্রথম?", ["w", "x", "y", "z"], labels="ABCD")
        qs, _ = self.run_page([d], [("৩১", "B")])
        self.assertEqual(qs[0]["correct_label"], "খ")
        self.assertEqual([o["shown"] for o in qs[0]["options"]], ["A", "B", "C", "D"])

    def test_structure_and_math_flags(self):
        qs, _ = self.run_page([q("1", "", ["a", "b", "c"], r"x \( \frac{1}", has_figure=True)], [("1", "ক")])
        self.assertTrue({"empty_stem", "option_count", "latex", "figure_check"} <= self.codes(qs[0]))


class ConfigLayers(unittest.TestCase):
    def test_settings_json_layers_over_config_toml_and_survives_damage(self):
        root = tmpdir(self)
        (root / "config.toml").write_text('[gemini]\nlayout_model = "from-toml"\nrequests_per_minute = 7\n', encoding="utf-8")
        self.assertEqual(load_config(root).get("gemini", "layout_model"), "from-toml")
        save_settings(root, {"gemini": {"layout_model": "from-json"}, "subjects": {"Only": "one"}})
        cfg = load_config(root)
        self.assertEqual((cfg.get("gemini", "layout_model"), cfg.get("gemini", "requests_per_minute")), ("from-json", 7))
        self.assertEqual(cfg.subjects, {"Only": "one"})                     # lists/dicts replace, they are not merged
        (root / "settings.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(load_config(root).get("gemini", "layout_model"), "from-toml")     # damaged file is ignored
        reset_settings(root)
        self.assertFalse((root / "settings.json").exists())

    def test_defaults_are_not_shared_between_configs(self):
        a, b = make_cfg(tmpdir(self)), make_cfg(tmpdir(self))
        a.raw["gemini"]["layout_model"] = "x"
        self.assertNotEqual(b.get("gemini", "layout_model"), "x")
        self.assertNotEqual(DEFAULTS["gemini"]["layout_model"], "x")

    def test_shipped_config_toml(self):
        from pathlib import Path
        cfg = load_config(Path(__file__).resolve().parents[1])
        self.assertEqual(len(set(cfg.extract_models)), 2)
        self.assertTrue(cfg.get("checks", "ai_fill") and cfg.get("checks", "math_check"))
        self.assertIsNone(cfg.get("gemini", "temperature"))


class Database(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self))
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)

    def card(self, n, stem, opts, correct="খ", flags=None):
        d = assemble.normalize_question(PageModel.model_validate({"questions": [q(n, stem, opts)]}).questions[0], DEFAULTS["subjects"])
        d["correct_label"], d["flags"] = correct, flags or []
        return d

    def ingest(self, card, exam_id, printed, status="ok"):
        pid = self.db.add_photo(f"sha{printed}{card['number']}", "x.jpg", "x", 90)
        page = self.db.add_page(pid, 0, printed, exam_id, False, {}, [])
        return self.db.ingest_question(card, page_id=page, photo_id=pid, exam_id=exam_id, printed_page=printed, status=status)

    def test_exact_duplicates_merge_and_remember_both_exams(self):
        e1, e2 = self.db.get_or_create_exam("Exam One", "2024", {}), self.db.get_or_create_exam("Exam Two", "2023", {})
        a, o1 = self.ingest(self.card(7, "Same question?", ["w", "x", "y", "z"]), e1, 100)
        b, o2 = self.ingest(self.card(31, "same question", ["z", "y", "x", "w"], correct="গ"), e2, 250)
        self.assertEqual((o1, o2, a), ("inserted", "duplicate", b))
        self.assertEqual(len(self.db.appearances(a)), 2)

    def test_conflicting_answer_reopens_the_question(self):
        e = self.db.get_or_create_exam("E", "", {})
        a, _ = self.ingest(self.card(1, "Q?", ["w", "x", "y", "z"], "খ"), e, 1)
        self.ingest(self.card(2, "Q?", ["w", "x", "y", "z"], "গ"), e, 2)
        d = self.db.get_question(a)
        self.assertEqual(d["status"], "review")
        self.assertIn("answer_conflict", {f["code"] for f in d["flags"]})

    def test_exam_is_inherited_from_the_previous_page_later(self):
        e = self.db.get_or_create_exam("E", "", {})
        p1 = self.db.add_photo("s1", "a.jpg", "a", 90)
        self.db.add_page(p1, 0, 11, None, False, {}, [])
        self.assertEqual(self.db.resolve_exams(), 0)
        p2 = self.db.add_photo("s2", "b.jpg", "b", 90)
        self.db.add_page(p2, 0, 10, e, False, {}, [])
        self.assertEqual(self.db.resolve_exams(), 1)
        self.assertEqual(self.db.exam_of_printed_page(11), e)

    def test_old_database_gets_new_columns_and_usage_and_key_state_work(self):
        path = self.cfg.root / "old.sqlite"
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE photos(id INTEGER PRIMARY KEY, sha256 TEXT, path TEXT, stem TEXT, rotate INTEGER, status TEXT, error TEXT, layout_json TEXT, workdir TEXT, created_at TEXT);"
                          "CREATE TABLE questions(id INTEGER PRIMARY KEY, guid TEXT UNIQUE, page_id INTEGER, col_idx INTEGER, number INTEGER, exam_id INTEGER, subject TEXT, stem TEXT, norm_stem TEXT, options_json TEXT, correct_label TEXT, key_raw TEXT, explanation TEXT, ai_explanation TEXT, figure_path TEXT, crop_path TEXT, flags_json TEXT, status TEXT, content_hash TEXT, alt_json TEXT, verify_json TEXT, exported_at TEXT, created_at TEXT, updated_at TEXT);")
        con.close()
        old = DB(path)
        cols = {r["name"] for r in old.conn.execute("PRAGMA table_info(questions)")}
        self.assertTrue({"answer_source", "explanation_original"} <= cols)
        old.close()
        self.db.add_usage(quota_day(), "m", 2, 100, 50)
        self.db.add_usage(quota_day(), "m", 1, 10, 5)
        self.assertEqual([(r["calls"], r["input_tokens"]) for r in self.db.usage_for(quota_day())], [(3, 110)])
        self.db.set_key_state("k", "bad", "invalid")
        self.assertEqual(self.db.key_states(), {"k": ("bad", "invalid")})
        self.db.clear_key_states()
        self.assertEqual(self.db.key_states(), {})


if __name__ == "__main__":
    unittest.main()

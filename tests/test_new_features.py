"""Photo-type detection, AI proofreading, the free formula tidy-up and automatic backups."""
from __future__ import annotations

import time
import unittest

from helpers import FakeClient, make_cfg, page, photo, q, tmpdir
from qb2anki import export_anki, maintenance, mathcheck
from qb2anki.db import DB
from qb2anki.pipeline import Pipeline
from qb2anki.proofread import proofread_database
from qb2anki.review import Review

BROKEN = "ধরি,\n\\therefore ব্যাসার্ধ \\(= \\frac{8r}{2}\\)\n„ „ \\(= 4r\\)"
FIXED = "ধরি,\n\\(\\therefore\\) ব্যাসার্ধ \\(= \\frac{4r}{2}\\)\nব্যাসার্ধ \\(= 2r\\)"


def circle_question():
    return q(7, "বৃত্তের ব্যাস দ্বিগুণ করা হলে ক্ষেত্রফল কত গুণ হবে?", ["২ গুণ", "৩ গুণ", "৪ গুণ", "৮ গুণ"], BROKEN,
             answer_in_explanation="গ", subject="Math")


def proof_fixes(item):
    if "8r" in item["explanation"]:
        return {"id": item["id"], "stem": item["stem"], "options": item["options"], "explanation": FIXED,
                "changes": "৪ had been read as 8; ditto marks written out"}
    return None


class Detection(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self))
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)

    def run_with(self, detect, pages=None):
        fake = FakeClient(pages=pages or {"p0": page([q(1, "A?", expl="x", answer_in_explanation="ক")]), "p1": page([])}, detect=detect)
        rep = Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        return fake, rep

    def test_a_detected_screenshot_is_one_page_with_the_general_reader(self):
        photo(self.cfg.inbox / "shot.png", (900, 600))
        fake, rep = self.run_with({"kind": "screenshot", "rotate_ccw_degrees": "0"})
        self.assertEqual(fake.calls, ["detect", "page p0"])                     # no layout request
        row = self.db.photo_by_stem("shot")
        self.assertEqual((row["kind"], row["reader"], row["rotate"], rep["photos"]), ("screenshot", "general", 0, 1))

    def test_a_detected_sideways_spread_is_turned_and_split_into_pages(self):
        photo(self.cfg.inbox / "spread.jpg", (900, 1200))                     # portrait photo of a landscape spread
        fake, rep = self.run_with({"kind": "spread", "rotate_ccw_degrees": "90"})
        self.assertEqual(fake.calls, ["detect", "layout", "page p0", "page p1"])
        row = self.db.photo_by_stem("spread")
        self.assertEqual((row["kind"], row["reader"], row["rotate"]), ("spread", "book", 90))
        from PIL import Image
        with Image.open(self.cfg.resolve(row["workdir"]) / "spread.jpg") as im:
            self.assertEqual(im.size, (1200, 900))

    def test_a_single_page_photo(self):
        photo(self.cfg.inbox / "page.jpg", (900, 1200))
        fake, _ = self.run_with({"kind": "page", "rotate_ccw_degrees": "180"})
        row = self.db.photo_by_stem("page")
        self.assertEqual((row["kind"], row["reader"], row["rotate"]), ("page", "general", 180))
        self.assertNotIn("layout", fake.calls)

    def test_a_wrongly_chosen_type_is_corrected_by_finding_the_right_turn(self):
        photo(self.cfg.inbox / "book" / "x.jpg", (1200, 900))                  # chose "book" (turn 90°) for an upright photo
        not_upright = {"text_upright": False, "pages": [{"printed_page": 1, "page_box": [5, 4, 960, 498]}]}

        class Fake(FakeClient):
            def generate_json(s, **kw):
                if kw["label"] == "layout" and kw["image"].width < kw["image"].height:
                    s.calls.append("layout")
                    return not_upright                                         # the 90° turn was wrong
                if kw["label"] == "orientation":
                    s.calls.append("orientation")
                    return {"rotate_ccw_degrees": "0"}
                return super().generate_json(**kw)
        fake = Fake(pages={"p0": page([q(1, "A?", expl="x", answer_in_explanation="ক")]), "p1": page([])})
        rep = Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        self.assertEqual((rep["photos"], rep["failed"]), (1, []))
        self.assertIn("orientation", fake.calls)
        self.assertEqual(self.db.photo_by_stem("x")["rotate"], 0)

    def test_failed_photos_can_be_read_again_with_detection(self):
        photo(self.cfg.inbox / "book" / "s.png", (900, 600))                   # a screenshot put under "book"
        bad = {"text_upright": True, "pages": [{"page_box": [0, 0, 100, 100]}]}
        fake = FakeClient(layout=bad, detect={"kind": "screenshot", "rotate_ccw_degrees": "0"},
                          pages={"p0": page([q(1, "A?", expl="x", answer_in_explanation="ক")])})
        pipe = Pipeline(self.cfg, self.db, fake, say=lambda *_: None)
        self.assertEqual(len(pipe.run()["failed"]), 1)
        rep = pipe.run(pipe.pending(), profile="auto")
        self.assertEqual((rep["photos"], rep["failed"]), (1, []))
        self.assertEqual(self.db.photo_by_stem("s")["kind"], "screenshot")


class Proofreading(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self), proofread=True)
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)
        photo(self.cfg.inbox / "screenshot" / "m.png", (900, 600))

    def test_misread_digit_and_ditto_marks_are_corrected_and_the_first_reading_kept(self):
        fake = FakeClient(pages={"p0": page([circle_question(), q(8, "Fine?", expl="ok", answer_in_explanation="ক")])}, proof_fn=proof_fixes)
        Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        self.assertEqual([c for c in fake.calls if c.startswith("proof")], ["proof p0"])     # 1 request per page
        c = {x["number"]: x for x in self.db.list_questions()}
        self.assertEqual(c[7]["explanation"], FIXED)
        self.assertEqual(c[7]["proof"]["explanation"], mathcheck.tidy(BROKEN))
        self.assertIn("৪", c[7]["proof"]["note"])
        self.assertIn("proofread", {f["code"] for f in c[7]["flags"]})
        self.assertEqual(c[7]["status"], "ok")                                   # a correction is a note, not a block
        self.assertIsNone(c[8]["proof"])
        self.assertTrue(Review(self.cfg, self.db).detail(c[7]["id"])["proof"])

    def test_proofreading_off_makes_no_request(self):
        self.cfg.raw["checks"]["proofread"] = False
        fake = FakeClient(pages={"p0": page([circle_question()])}, proof_fn=proof_fixes)
        Pipeline(self.cfg, self.db, fake, say=lambda *_: None).run()
        self.assertFalse([c for c in fake.calls if c.startswith("proof")])

    def test_stored_cards_can_be_proofread_later_and_become_exportable_again(self):
        self.cfg.raw["checks"]["proofread"] = False
        Pipeline(self.cfg, self.db, FakeClient(pages={"p0": page([circle_question()])}), say=lambda *_: None).run()
        self.assertEqual(export_anki.export(self.cfg, self.db)["count"], 1)
        r = proofread_database(self.cfg, self.db, FakeClient(proof_fn=proof_fixes), "m")
        self.assertEqual((r["pages"], r["changed"]), (1, 1))
        card = self.db.list_questions()[0]
        self.assertEqual((card["explanation"], card["exported_at"]), (FIXED, None))
        text = export_anki.export(self.cfg, self.db)["path"].read_text(encoding="utf-8")
        self.assertIn("frac{4r}{2}", text)
        r2 = proofread_database(self.cfg, self.db, FakeClient(), "m")              # nothing more to fix
        self.assertEqual(r2["changed"], 0)
        self.assertIsNotNone(self.db.list_questions()[0]["exported_at"])          # not marked for export again

    def test_approved_cards_are_never_touched(self):
        self.cfg.raw["checks"]["proofread"] = False
        Pipeline(self.cfg, self.db, FakeClient(pages={"p0": page([circle_question()])}), say=lambda *_: None).run()
        cid = self.db.list_questions()[0]["id"]
        self.db.update_question(cid, status="approved")
        self.assertEqual(proofread_database(self.cfg, self.db, FakeClient(proof_fn=proof_fixes), "m")["pages"], 0)


class Tidy(unittest.TestCase):
    def test_stray_commands_are_wrapped_and_formulas_untouched(self):
        self.assertEqual(mathcheck.tidy(r"\therefore ক্ষেত্রফল \(= \pi r^2\)"), r"\(\therefore\) ক্ষেত্রফল \(= \pi r^2\)")
        self.assertEqual(mathcheck.tidy(r"\(x\) \times 3 = \frac{1}{2}"), r"\(x\) \(\times\) 3 = \(\frac{1}{2}\)")
        self.assertEqual(mathcheck.tidy(r"\frac{1}{2 broken"), r"\frac{1}{2 broken")         # left for the AI
        self.assertEqual(mathcheck.tidy("সাধারণ লেখা"), "সাধারণ লেখা")
        self.assertEqual(mathcheck.tidy(r"\pin is not a command"), r"\pin is not a command")

    def test_export_tidies_cards_that_were_stored_before(self):
        cfg = make_cfg(tmpdir(self))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        import json
        opts = json.dumps([{"label": l, "shown": l, "text": t} for l, t in zip("কখগঘ", "wxyz")])
        db.conn.execute("INSERT INTO questions(guid,subject,stem,options_json,correct_label,status,flags_json,explanation) VALUES(?,?,?,?,?,?,?,?)",
                        ("g1", "Math", "Q?", opts, "খ", "ok", "[]", "\\therefore x = 2"))
        db.commit()
        text = export_anki.export(cfg, db)["path"].read_text(encoding="utf-8")
        self.assertIn("\\(\\therefore\\) x = 2", text)


class AutoBackup(unittest.TestCase):
    def test_auto_backup_keeps_the_newest_and_never_deletes_your_own(self):
        cfg = make_cfg(tmpdir(self))
        cfg.raw["backup"] = {"auto": True, "keep": 3}
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        self.assertIsNone(maintenance.auto_backup(cfg, db))                     # empty database: nothing to back up
        db.conn.execute("INSERT INTO questions(guid,stem,status,flags_json) VALUES('g','Q','ok','[]')")
        db.commit()
        mine = maintenance.backup_db(cfg, db)
        for _ in range(5):
            maintenance.auto_backup(cfg, db)
            time.sleep(0.01)
        names = [b["name"] for b in maintenance.list_backups(cfg)]
        self.assertEqual(sum(n.startswith("auto_") for n in names), 3)
        self.assertIn(mine.name, names)
        cfg.raw["backup"]["auto"] = False
        self.assertIsNone(maintenance.auto_backup(cfg, db))


if __name__ == "__main__":
    unittest.main()

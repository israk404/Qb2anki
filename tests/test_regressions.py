"""One test per bug found in the final review, so they stay fixed."""
from __future__ import annotations

import json
import shutil
import socket
import unittest
from pathlib import Path

from helpers import FakeClient, make_cfg, page, photo, q, tmpdir
from PIL import Image

from qb2anki import export_anki, imaging
from qb2anki.db import DB
from qb2anki.pipeline import Pipeline
from qb2anki.review import Review
from qb2anki.schemas import LayoutModel, PageModel


def pages_for(n_questions=1):
    return {"p0": page([q(i, f"Question {i}?", expl="x", answer_in_explanation="খ") for i in range(1, n_questions + 1)])}


class MovingTheFolder(unittest.TestCase):
    def test_review_pictures_and_rebuild_still_work_after_moving_the_whole_folder(self):
        a = tmpdir(self) / "app"
        cfg = make_cfg(a)
        photo(cfg.inbox / "screenshot" / "BCS" / "s.png", (900, 600))
        db = DB(cfg.db_path)
        Pipeline(cfg, db, FakeClient(pages=pages_for()), say=lambda *_: None).run()
        row = db.photo_by_stem("s")
        self.assertFalse(Path(row["path"]).is_absolute() or Path(row["workdir"]).is_absolute())      # stored relative
        db.close()
        b = tmpdir(self) / "moved"
        shutil.copytree(a, b)
        shutil.rmtree(a)
        cfg2 = make_cfg(b)
        db2 = DB(cfg2.db_path)
        self.addCleanup(db2.close)
        qid = db2.list_questions()[0]["id"]
        d = Review(cfg2, db2).detail(qid)
        self.assertTrue(d["crop_url"] and d["context_url"] and d["overlay_url"])
        self.assertTrue((cfg2.work / d["context_url"][len("/file/"):]).exists())
        rep = Pipeline(cfg2, db2, FakeClient(pages=pages_for()), say=lambda *_: None).rerun(row["id"], force=True)
        self.assertEqual(rep["photos"], 1)

    def test_old_absolute_paths_are_re_anchored(self):
        cfg = make_cfg(tmpdir(self))
        (cfg.work / "x" / "crops").mkdir(parents=True)
        (cfg.inbox / "book").mkdir(parents=True)
        (cfg.inbox / "book" / "p.jpg").write_bytes(b"x")
        old = cfg.root.parent / "old place" / "QB"                # an absolute path that no longer exists (any OS)
        self.assertEqual(cfg.resolve(str(old / "work" / "x")), cfg.work / "x")
        self.assertEqual(cfg.resolve(str(old / "inbox" / "book" / "p.jpg")), cfg.inbox / "book" / "p.jpg")
        self.assertEqual(cfg.resolve("work/x"), cfg.root / "work" / "x")


class SameFileNames(unittest.TestCase):
    def test_rebuild_redoes_every_photo_even_with_the_same_file_name(self):
        cfg = make_cfg(tmpdir(self))
        photo(cfg.inbox / "screenshot" / "Exam A" / "image.png", (900, 600))
        photo(cfg.inbox / "screenshot" / "Exam B" / "image.png", (950, 600))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        n = iter(range(100))

        class Two(FakeClient):
            def generate_json(s, **kw):
                if kw["label"].startswith("page"):
                    i = next(n)
                    return page([q(1, f"Different question {i}?", expl="x", answer_in_explanation="খ")])
                return super().generate_json(**kw)
        pipe = Pipeline(cfg, db, Two(), say=lambda *_: None)
        pipe.run()
        rows = db.conn.execute("SELECT * FROM photos").fetchall()
        self.assertEqual(len({r["workdir"] for r in rows}), 2)
        for r in rows:
            self.assertEqual(pipe.rerun(r["id"])["photos"], 1)
        exams = {db.exam_display(c["exam_id"]) for c in db.list_questions()}
        self.assertEqual(exams, {"Exam A", "Exam B"})                         # both photos, each with its own exam


class BigCollections(unittest.TestCase):
    def test_export_has_no_hidden_5000_card_limit(self):
        cfg = make_cfg(tmpdir(self))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        opts = json.dumps([{"label": l, "shown": l, "text": t} for l, t in zip("কখগঘ", "wxyz")])
        db.conn.executemany("INSERT INTO questions(guid,subject,stem,options_json,correct_label,status,flags_json) VALUES(?,?,?,?,?,?,?)",
                            [(f"g{i}", "English", f"Q{i}?", opts, "খ", "ok", "[]") for i in range(5005)])
        db.commit()
        self.assertEqual(export_anki.export(cfg, db)["count"], 5005)


class OddModelOutput(unittest.TestCase):
    def test_nulls_numbers_and_bengali_digits_do_not_sink_a_page(self):
        pm = PageModel.model_validate({"questions": [None, {"number": 12, "stem": None, "options": [{"label": None, "text": "x"}, None],
                                                            "explanation": None, "has_figure": None, "subject": None, "figure_box": None}],
                                       "answer_key": [{"number": 12, "answer": None}, None], "exam_header": {"title": None}})
        self.assertEqual((len(pm.questions), pm.questions[0].number, pm.questions[0].stem, pm.questions[0].has_figure), (1, "12", "", False))
        self.assertEqual(pm.answer_key[0].answer, "")
        lm = LayoutModel.model_validate({"text_upright": True, "pages": [{"printed_page": "৫৬৮", "page_box": None}]})
        self.assertEqual(lm.pages[0].printed_page, 568)
        pages, problems = imaging.validate_layout(lm, 1000, 1000)
        self.assertTrue(problems)                                             # reported as a problem, not a crash

    def test_a_page_with_nulls_is_still_stored(self):
        cfg = make_cfg(tmpdir(self))
        photo(cfg.inbox / "screenshot" / "s.png", (900, 600))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        odd = {"questions": [{"number": 3, "stem": "Q?", "options": [{"label": "ক", "text": "a"}, {"label": "খ", "text": None}],
                              "explanation": None, "answer_in_explanation": None, "has_figure": None, "subject": "English"}],
               "answer_key": None, "exam_header": None}
        rep = Pipeline(cfg, db, FakeClient(pages={"p0": odd}), say=lambda *_: None).run()
        self.assertEqual((rep["photos"], rep["failed"]), (1, []))
        self.assertEqual(db.list_questions()[0]["status"], "review")             # flagged (empty option, missing answer), not lost

    def test_an_unexpected_error_fails_one_photo_and_the_others_continue(self):
        cfg = make_cfg(tmpdir(self))
        photo(cfg.inbox / "a.jpg")
        photo(cfg.inbox / "b.jpg", (1300, 900))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        first = []

        class Odd(FakeClient):
            def generate_json(s, **kw):
                if kw["label"] == "layout" and not first:
                    first.append(1)
                    return {"text_upright": True, "pages": "this is not a list"}       # invalid: a validation error
                return super().generate_json(**kw)
        fake = Odd(pages={"p0": page([q(1, "A?", expl="x")], [(1, "ক")]), "p1": page([])})
        rep = Pipeline(cfg, db, fake, say=lambda *_: None).run()
        self.assertEqual((len(rep["failed"]), rep["photos"]), (1, 1))
        self.assertIn("unexpected problem", rep["failed"][0][1])


class Images(unittest.TestCase):
    def test_transparent_png_gets_a_white_background(self):
        p = tmpdir(self) / "t.png"
        im = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        im.putpixel((5, 5), (0, 0, 0, 255))
        im.save(p)
        out = imaging.load_image(p)
        self.assertEqual((out.mode, out.getpixel((0, 0)), out.getpixel((5, 5))), ("RGB", (255, 255, 255), (0, 0, 0)))


class YourAnswerIsYours(unittest.TestCase):
    def test_changing_an_ai_answer_removes_the_ai_label(self):
        cfg = make_cfg(tmpdir(self), ai_fill=True)
        photo(cfg.inbox / "screenshot" / "s.png", (900, 600))
        db = DB(cfg.db_path)
        self.addCleanup(db.close)
        Pipeline(cfg, db, FakeClient(pages={"p0": page([q(1, "Q?", expl="x")])}, fill_answer="খ"), say=lambda *_: None).run()
        c = db.list_questions()[0]
        self.assertEqual(c["answer_source"], "ai")
        rv = Review(cfg, db)
        rv.save(c["id"], {"action": "approve", "stem": c["stem"], "options": c["options"], "correct_label": "খ", "explanation": "x"})
        self.assertEqual(db.get_question(c["id"])["answer_source"], "ai")      # approved unchanged: still labelled AI
        rv.save(c["id"], {"action": "approve", "stem": c["stem"], "options": c["options"], "correct_label": "গ", "explanation": "x"})
        self.assertEqual(db.get_question(c["id"])["answer_source"], "user")    # you changed it: your answer
        text = export_anki.export(cfg, db)["path"].read_text(encoding="utf-8")
        self.assertNotIn("Answer worked out by AI", text)
        self.assertNotIn("Answer::AI", text)


class AppStartTwice(unittest.TestCase):
    def test_second_start_does_not_crash(self):
        import io
        from contextlib import redirect_stdout
        from qb2anki.config import save_settings
        from qb2anki.ui import serve
        root = tmpdir(self)
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        save_settings(root, {"ui": {"port": busy.getsockname()[1]}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertFalse(serve(root, open_browser=False))
        self.assertIn("already in use", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

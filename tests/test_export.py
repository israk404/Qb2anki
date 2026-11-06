"""The Anki file: format, card design, labels, and a real import with Anki's own engine."""
from __future__ import annotations

import csv
import os
import re
import tempfile
import unittest
from pathlib import Path

from helpers import DEFAULTS, make_cfg, q, tmpdir
from qb2anki import assemble, export_anki
from qb2anki.db import DB
from qb2anki.schemas import PageModel

try:
    from anki.collection import Collection, CsvMetadata, ImportCsvRequest
except Exception:  # noqa: BLE001
    Collection = None


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tmpdir(self))
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)

    def add(self, number, stem, opts=("w", "x", "y", "z"), correct="খ", printed=5, exam="Exam One 2024", status="ok", labels="কখগঘঙ",
            expl="উত্তরের ব্যাখ্যা \\(x^2\\) & <b>x</b>", **extra):
        c = assemble.normalize_question(PageModel.model_validate({"questions": [q(number, stem, opts, expl, labels=labels)]}).questions[0], DEFAULTS["subjects"])
        c.update(correct_label=correct, flags=[], **extra)
        e = self.db.get_or_create_exam(exam, "", {}) if exam else None
        if e:
            self.db.set_exam_label(e, exam)
        pid = self.db.add_photo(f"sha{printed}{number}", "x.jpg", "x", 90)
        pg = self.db.add_page(pid, 0, printed, e, False, {}, [])
        return self.db.ingest_question(c, page_id=pg, photo_id=pid, exam_id=e, printed_page=printed, status=status)[0]

    def rows(self, path):
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines, list(csv.reader([l for l in lines if not l.startswith("#")], delimiter="\t"))


class Format(Base):
    def test_file_format_and_only_ok_cards_once(self):
        self.add(1, "Q one?")
        self.add(2, "Q two?", status="review")
        r = export_anki.export(self.cfg, self.db)
        self.assertEqual(r["count"], 1)
        lines, rows = self.rows(r["path"])
        self.assertEqual((lines[0], "#tags column:6" in lines, len(rows)), ("#separator:tab", True, 1))
        guid, nt, deck, front, back, tags = rows[0]
        self.assertEqual((nt, deck), ("Basic", "Job Questions::English"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", back)                                   # HTML in the source text is escaped
        self.assertIn("\\(x^2\\)", back)                                                # math delimiters survive
        for t in tags.split(" "):
            self.assertRegex(t, r"^(Subject::|Exam::|page_)\S+$")
        self.assertEqual(export_anki.export(self.cfg, self.db)["count"], 0)             # never exported twice
        self.assertEqual(export_anki.export(self.cfg, self.db, include_all=True)["count"], 1)

    def test_card_design_lives_in_the_front_and_supports_night_mode(self):
        self.add(1, "প্রশ্ন?", subject="General Knowledge") if False else self.add(1, "প্রশ্ন?")
        _, rows = self.rows(export_anki.export(self.cfg, self.db)["path"])
        front, back = rows[0][3], rows[0][4]
        self.assertTrue(front.startswith("<style>"))
        self.assertIn(".nightMode .qb", front)
        self.assertIn('class="qb-opt"', front)
        self.assertIn('class="qb-chip"', front)
        self.assertNotIn("<style>", back)                                               # the back shows under the front: no duplicate CSS
        self.assertRegex(back, r'class="qb-ans"><span class="qb-l">খ</span>')
        self.assertIn('class="qb-exp"', back)

    def test_labels_as_printed_ai_markers_and_source_line(self):
        self.add(1, "Q?", labels="ABCD", correct="খ", answer_source="ai", expl="")
        e2 = self.db.get_or_create_exam("Other Exam 2023", "", {})
        self.db.set_exam_label(e2, "Other Exam 2023")
        self.add(2, "Q two?", answer_source=None, ai_explanation="AI wrote this", expl="")
        _, rows = self.rows(export_anki.export(self.cfg, self.db)["path"])
        by = {r[3].split("qb-q\">")[1].split("<")[0]: r for r in rows}
        r1, r2 = by["Q?"], by["Q two?"]
        self.assertIn('<span class="qb-l">A</span>', r1[3])
        self.assertRegex(r1[4], r'class="qb-ans"><span class="qb-l">B</span>')
        self.assertIn("Answer worked out by AI", r1[4])
        self.assertIn("Answer::AI", r1[5])
        self.assertIn("AI-generated explanation", r2[4])
        self.assertNotIn("Answer worked out by AI", r2[4])

    def test_source_line_modes_and_page_tag(self):
        self.add(7, "Same question?", printed=531, exam="Insurance Manager 2024")
        self.add(31, "Same question?", opts=("z", "y", "x", "w"), correct="গ", printed=250, exam="Bank Officer 2023")
        def render(mode, **extra):
            self.cfg.raw["anki"].update({"source_line": mode, **extra})
            text = export_anki.export(self.cfg, self.db, include_all=True)["path"].read_text(encoding="utf-8")
            m = re.search(r'qb-src""?>([^<]*)<', text)          # the file is CSV-quoted: attribute quotes are doubled
            return (m.group(1) if m else None), text.splitlines()[-1].rsplit("\t", 1)[-1]
        src, tags = render("exam")
        self.assertEqual(src, "Insurance Manager 2024 · also asked in: Bank Officer 2023")
        self.assertNotIn("page_", tags)
        self.assertIn("p.531 · Q7", render("full")[0])
        self.assertIsNone(render("none")[0])
        self.assertIn("page_531", render("exam", page_tag=True)[1])

    def test_preview_and_media_folder_discovery(self):
        qid = self.add(1, "Q?")
        d = export_anki.preview(self.cfg, self.db, qid)
        self.assertIn("qb-opt", d["front"])
        self.assertIn("qb-ans", d["back"])
        base = tmpdir(self) / "Anki2" / "User 1" / "collection.media"
        base.mkdir(parents=True)
        os.environ["APPDATA"] = str(base.parents[2])
        self.addCleanup(os.environ.pop, "APPDATA", None)
        self.assertIn(str(base), export_anki.find_anki_media_dirs())


@unittest.skipIf(Collection is None, "the anki library is not installed")
class RealAnkiImport(Base):
    def import_file(self, col, path):
        meta = col.get_csv_metadata(str(path), None)
        meta.dupe_resolution = CsvMetadata.DupeResolution.UPDATE          # the dialog's default: Existing notes = Update
        return col.import_csv(ImportCsvRequest(path=str(path), metadata=meta)).log

    def test_anki_accepts_the_file_and_refreshes_cards_on_reimport(self):
        self.add(1, "Q one?", exam="Insurance Manager 2024")
        self.add(2, "বাংলা প্রশ্ন?", exam="Insurance Manager 2024", subject="Bangla") if False else self.add(2, "বাংলা প্রশ্ন?", exam="Insurance Manager 2024")
        path = export_anki.export(self.cfg, self.db)["path"]
        col = Collection(os.path.join(tempfile.mkdtemp(), "collection.anki2"))
        self.addCleanup(col.close)
        log = self.import_file(col, path)
        self.assertEqual((log.found_notes, len(log.new), len(log.missing_notetype)), (2, 2, 0))
        self.assertIn("Job Questions::English", [d.name for d in col.decks.all_names_and_ids()])
        note = col.get_note(col.find_notes("")[0])
        self.assertIn("qb-opt", note.fields[0])
        self.assertTrue(note.fields[1].startswith('<div class="qb">'))
        self.assertTrue(any(t.startswith("Exam::") for t in note.tags))
        # importing the same file twice only skips; a changed card is updated in place without duplicates
        self.assertEqual(len(self.import_file(col, path).duplicate), 2)
        self.cfg.raw["anki"]["source_line"] = "none"
        again = export_anki.export(self.cfg, self.db, include_all=True)["path"]
        log = self.import_file(col, again)
        self.assertEqual((len(log.updated), len(log.new), len(col.find_notes(""))), (2, 0, 2))

    def test_a_missing_note_type_is_reported_as_anki_does(self):
        self.add(1, "Q?")
        self.cfg.raw["anki"]["notetype"] = "No Such Type"
        path = export_anki.export(self.cfg, self.db)["path"]
        col = Collection(os.path.join(tempfile.mkdtemp(), "collection.anki2"))
        self.addCleanup(col.close)
        log = self.import_file(col, path)
        self.assertEqual((len(log.new), len(log.missing_notetype)), (0, 1))


if __name__ == "__main__":
    unittest.main()

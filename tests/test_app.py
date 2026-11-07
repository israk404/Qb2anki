"""The app server: protection, upload, background jobs, settings, keys, export, database tools."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from helpers import FakeClient, page, photo, q, tmpdir
from qb2anki.ui import App, clean_patch, make_handler


class AppTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        from qb2anki.config import save_settings
        save_settings(self.root, {"checks": {"ai_fill": False, "math_check": False, "fact_check": False,
                                             "proofread": False}})                       # these tests want a card without an answer
        self.fake = FakeClient(pages={"p0": page([q(1, "Question one?", ["a", "b", "c", "d"], answer_in_explanation="খ", expl="because"),
                                                  q(2, "Question two?")], [])})
        self.app = App(self.root, client_factory=lambda cfg, db: self.fake)
        import os
        os.environ["QB_NO_OPEN"] = "1"
        self.addCleanup(os.environ.pop, "QB_NO_OPEN", None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.app.httpd = self.httpd
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close(), self.app.db.close()))

    # -- helpers
    def req(self, path, body=None, token=True, headers=None, raw=None, host=None):
        h = {"X-QB-Token": self.app.token} if token else {}
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        if host:
            r.add_unredirected_header("Host", host)
        try:
            with urllib.request.urlopen(r) as resp:
                raw_out = resp.read()
                return resp.status, (json.loads(raw_out) if resp.headers.get("Content-Type", "").startswith("application/json") else raw_out)
        except urllib.error.HTTPError as e:
            raw_out = e.read()
            try:
                return e.code, json.loads(raw_out)
            except ValueError:
                return e.code, raw_out

    def wait_job(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            _, j = self.req("/api/job")
            if j.get("status") not in ("running", "none"):
                return j
            time.sleep(0.05)
        self.fail("job did not finish")

    def upload(self, name="quiz.png", profile="screenshot", exam="BCS 45", size=(900, 600)):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", size, "white").save(buf, format="PNG")
        from urllib.parse import quote               # the app's screen sends these encodeURIComponent()-encoded
        return self.req("/api/upload", raw=buf.getvalue(), headers={"X-Filename": quote(name), "X-Profile": quote(profile), "X-Exam": quote(exam)})

    # -- protection
    def test_api_needs_the_token_and_the_right_host_and_paths_are_confined(self):
        self.assertEqual(self.req("/api/status", token=False)[0], 403)
        self.assertEqual(self.req("/api/status", headers={"X-QB-Token": "wrong"})[0], 403)
        self.assertEqual(self.req("/api/status", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("/api/settings", {"gemini": {}}, token=False)[0], 403)
        self.assertEqual(self.req("/api/status")[0], 200)
        page_html = self.req("/", token=False)[1].decode()
        self.assertIn(self.app.token, page_html)                                      # the page carries the token, nothing else does
        self.assertIn(self.req("/file/../../data/qb.sqlite", token=False)[0], (403, 404))
        self.assertIn(self.req("/download/../data/qb.sqlite", token=False)[0], (403, 404))

    # -- upload and inbox
    def test_a_browser_that_hangs_up_early_is_not_an_error(self):
        class Gone:
            def write(self, b):
                raise ConnectionAbortedError(10053, "aborted by the software in your host machine")
            def flush(self):
                pass
        H = make_handler(self.app)
        h = H.__new__(H)
        h.wfile, h.request_version, h.requestline, h.command = Gone(), "HTTP/1.1", "GET /api/job HTTP/1.1", "GET"
        h.client_address, h.close_connection = ("127.0.0.1", 1), False
        h._json({"status": "none"})                                   # must not raise
        self.assertTrue(h.close_connection)

    def test_upload_lands_in_the_right_folder_and_rejects_non_images(self):
        s, r = self.upload("my photo.png", "screenshot", "BCS 45")
        self.assertEqual((s, r["path"]), (200, "screenshot/BCS 45/my photo.png"))
        s, r2 = self.upload("my photo.png", "screenshot", "BCS 45")
        self.assertEqual(r2["path"], "screenshot/BCS 45/my photo_1.png")                 # never overwrites
        s, r3 = self.upload("../../evil.png", "book", "")
        self.assertTrue(r3["path"].startswith("book/") and ".." not in r3["path"])
        s, r4 = self.req("/api/upload", raw=b"x", headers={"X-Filename": "notes.txt", "X-Profile": "book", "X-Exam": ""})
        self.assertEqual(s, 400)
        inbox = self.req("/api/inbox")[1]
        self.assertEqual({(f["profile"], f["exam"], f["status"]) for f in inbox}, {("screenshot", "BCS 45", "waiting"), ("book", "", "waiting")})

    # -- a whole reading job
    def test_reading_job_runs_in_the_background_and_fills_the_database(self):
        self.upload()
        s, r = self.req("/api/run", {"limit": "", "rotate": ""})
        self.assertEqual(s, 200)
        self.assertEqual(self.req("/api/run", {})[0], 409)                                # one task at a time ... (or already finished)
        j = self.wait_job()
        self.assertEqual(j["status"], "done")
        text = "\n".join(self.req("/api/job?since=0")[1]["lines"])
        self.assertIn("Done: 1 photo(s) read, 2 new questions", text)
        st = self.req("/api/status")[1]
        self.assertEqual((st["inbox"]["waiting"], st["counts"]["review"]), (0, 1))        # question two has no answer
        usage = self.req("/api/status")[1]["usage"]
        self.assertIsInstance(usage, list)
        queue = self.req("/api/queue?f=all")[1]
        self.assertEqual(len(queue), 2)
        d = self.req(f"/api/question/{queue[0]['id']}")[1]
        self.assertTrue(d["crop_url"].startswith("/file/"))
        self.assertEqual(self.req(d["crop_url"], token=False)[0], 200)
        self.assertEqual(self.req("/api/run", {})[1]["ok"], True)                         # nothing waiting: a quick no-op job
        self.assertEqual(self.wait_job()["status"], "done")

    def test_job_can_be_stopped_and_errors_are_reported(self):
        for i in range(3):
            self.upload(f"p{i}.png", size=(900 + i, 600))
        started = threading.Event()
        orig = self.fake.generate_json

        def slow(**kw):
            started.set()
            time.sleep(0.15)
            return orig(**kw)
        self.fake.generate_json = slow
        self.req("/api/run", {})
        started.wait(5)
        self.req("/api/job/stop", {})
        j = self.wait_job()
        self.assertEqual(j["status"], "stopped")
        self.assertIn("Stopped by you", "\n".join(self.req("/api/job?since=0")[1]["lines"]))

        def boom(**kw):
            raise RuntimeError("kaboom")
        self.fake.generate_json = boom
        self.req("/api/run", {})
        j = self.wait_job()
        self.assertIn(j["status"], ("done", "error"))

    # -- settings and keys
    def test_settings_are_validated_saved_and_reset(self):
        self.assertEqual(self.req("/api/settings", {"anki": {"source_line": "weird"}})[0], 400)
        self.assertEqual(self.req("/api/settings", {"gemini": {"requests_per_minute": "-3"}})[0], 400)
        self.assertEqual(self.req("/api/settings", {"profile_rotate": {"book": "45"}})[0], 400)
        s, _ = self.req("/api/settings", {"gemini": {"layout_model": "new-model", "extract_models": "a\nb", "fallback_models": "x, y"},
                                         "checks": {"cross_check": True}, "anki": {"deck_root": "My Deck"}, "subjects": {"A": "x"},
                                         "profile_rotate": {"book": "auto"}, "evil": {"key": 1}})
        self.assertEqual(s, 200)
        cfg = self.req("/api/settings")[1]
        self.assertEqual((cfg["gemini"]["layout_model"], cfg["gemini"]["extract_models"], cfg["gemini"]["fallback_models"]),
                         ("new-model", ["a", "b"], ["x", "y"]))
        self.assertEqual((cfg["checks"]["cross_check"], cfg["anki"]["deck_root"], cfg["subjects"], cfg["profiles"]["book"]["rotate"]), (True, "My Deck", {"A": "x"}, "auto"))
        saved = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("evil", saved)                                                   # only whitelisted settings are ever written
        self.assertTrue(cfg["has_overrides"])
        self.req("/api/settings/reset", {})
        self.assertFalse(self.req("/api/settings")[1]["has_overrides"])
        self.assertEqual(clean_patch({"gemini": {"thinking_level": ""}}), {})

    def test_keys_and_main_key_are_stored_without_leaking(self):
        key = "AIza" + "S" * 34
        self.assertEqual(self.req("/api/apikey", {"key": "short"})[0], 400)
        self.assertEqual(self.req("/api/apikey", {"key": key})[1]["ok"], True)
        self.assertIn(f"GEMINI_API_KEY={key}", (self.root / ".env").read_text(encoding="utf-8"))
        self.assertTrue(self.req("/api/status")[1]["has_key"])
        info = self.req("/api/keys")[1]
        self.assertNotIn(key, json.dumps(info))                                           # only the last 4 characters come back
        self.assertEqual(info["main_hint"], "…" + key[-4:])
        two = "\n".join("AIza" + f"{i:02d}" + "x" * 30 for i in range(3))
        self.assertEqual(self.req("/api/keys", {"text": two})[1]["count"], 3)
        self.assertIsNone(self.req("/api/keys")[1]["rotation"])                          # "use one key" is the default
        self.assertEqual(self.req("/api/settings", {"gemini": {"key_mode": "both"}})[0], 400)
        self.assertEqual(self.req("/api/settings", {"gemini": {"key_mode": "rotate"}})[0], 200)
        self.assertEqual(self.req("/api/keys")[1]["rotation"]["keys"], 4)                 # the main key plus the list
        self.assertEqual(self.req("/api/status")[1]["mode"]["key_mode"], "rotate")
        self.assertEqual(self.req("/api/keys", {"text": ""})[1]["count"], 0)
        self.assertFalse((self.root / "keys.txt").exists())
        self.assertEqual(self.req("/api/keys/reset", {})[1]["ok"], True)

    # -- review, export, database
    def seed(self):
        self.upload()
        self.req("/api/run", {})
        self.wait_job()
        return self.req("/api/queue?f=all")[1]

    def test_review_export_and_database_tools(self):
        items = self.seed()
        good = next(i for i in items if i["number"] == 1)
        bad = next(i for i in items if i["number"] == 2)
        info = self.req("/api/export/info")[1]
        self.assertEqual((info["ready"], info["held"]), (1, 1))
        d = self.req(f"/api/question/{bad['id']}")[1]
        self.assertEqual(self.req(f"/api/question/{bad['id']}", {"action": "approve", "stem": d["stem"], "options": d["options"], "correct_label": None})[0], 400)
        self.assertEqual(self.req(f"/api/question/{bad['id']}", {"action": "approve", "stem": d["stem"], "options": d["options"], "correct_label": "গ"})[0], 200)
        self.assertEqual(self.req(f"/api/preview/{good['id']}")[1]["front"].count('class="qb-opt"'), 4)
        s, r = self.req("/api/export", {})
        self.assertEqual((s, r["count"]), (200, 2))
        self.assertEqual(self.req("/download/" + r["file"], token=False)[0], 200)
        self.assertEqual(self.req("/api/export", {})[1]["count"], 0)
        self.assertEqual(self.req("/api/export", {"anki_media": "/no/such/folder"})[0], 400)
        # database: backup, damage, restore
        name = self.req("/api/db/backup", {})[1]["name"]
        self.assertIn(name, [b["name"] for b in self.req("/api/backups")[1]])
        self.assertEqual(self.req("/api/db/reset", {"mode": "records", "confirm": "nope"})[0], 400)
        self.assertEqual(self.req("/api/db/reset", {"mode": "exported"})[0], 200)
        self.assertEqual(self.req("/api/export/info")[1]["ready"], 2)                     # re-export is possible again
        self.assertEqual(self.req("/api/db/reset", {"mode": "records", "confirm": "DELETE"})[0], 200)
        self.assertEqual(self.req("/api/queue?f=all")[1], [])
        self.assertEqual(self.req("/api/db/restore", {"name": name})[0], 200)
        self.assertEqual(len(self.req("/api/queue?f=all")[1]), 2)
        self.assertEqual(self.req("/api/db/restore", {"name": "../../etc/passwd"})[0], 404)
        self.assertEqual(self.req("/api/bundle", {"no_images": True})[1]["ok"], True)
        self.assertEqual(self.req("/api/open", {"what": "inbox"})[1]["ok"], True)
        self.assertEqual(self.req("/api/open", {"what": "nope"})[0], 400)

    def test_restore_right_after_backup_never_overwrites_the_backup(self):
        self.seed()
        name = self.req("/api/db/backup", {})[1]["name"]
        self.req("/api/db/reset", {"mode": "records", "confirm": "DELETE"})            # all within the same second
        self.assertEqual(self.req("/api/queue?f=all")[1], [])
        self.assertEqual(self.req("/api/db/restore", {"name": name})[0], 200)
        self.assertEqual(len(self.req("/api/queue?f=all")[1]), 2)
        names = [b["name"] for b in self.req("/api/backups")[1]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any(n.startswith("before_restore_") for n in names))

    def test_ai_fill_and_rebuild_and_doctor_jobs(self):
        self.seed()                                                                        # card two has no answer
        self.req("/api/fill", {})
        j = self.wait_job()
        self.assertEqual(j["status"], "done")
        self.assertIn("AI wrote 1 answer(s)", "\n".join(self.req("/api/job?since=0")[1]["lines"]))
        two = next(i for i in self.req("/api/queue?f=ai")[1])
        d = self.req(f"/api/question/{two['id']}")[1]
        self.assertEqual((d["correct_label"], d["answer_source"]), ("খ", "ai"))
        self.req("/api/doctor", {})
        self.assertEqual(self.wait_job()["status"], "done")
        self.req("/api/rebuild", {})
        self.assertEqual(self.wait_job()["status"], "done")
        self.req("/api/models/scan", {})
        j = self.wait_job()
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["suggest"]["layout_model"], "gemini-3.6-flash")
        self.assertNotIn("gemini-3.1-pro-preview", j["result"]["working"])               # the model that fails is not suggested
        (self.root / "keys.txt").write_text("\n".join(["AIza" + f"{i:02d}" + "x" * 30 for i in range(3)]), encoding="utf-8")
        self.req("/api/keys/check", {})
        self.assertIn("Result: 3 ok", "\n".join(self.req("/api/job?since=0")[1]["lines"]) if self.wait_job()["status"] == "done" else "")

    def test_pictures_load_when_the_exam_folder_name_has_special_characters(self):
        self.upload("প্রশ্ন #1 100%.png", "screenshot", "BCS #45 100% (২০২৪)")       # picture folders are named after the file
        self.req("/api/run", {})
        self.assertEqual(self.wait_job()["status"], "done")
        item = self.req("/api/queue?f=all")[1][0]
        d = self.req(f"/api/question/{item['id']}")[1]
        self.assertIn("%23", d["crop_url"])
        self.assertIn("%25", d["crop_url"])
        self.assertEqual(self.req(d["crop_url"], token=False)[0], 200)
        self.assertEqual(self.req(d["context_url"], token=False)[0], 200)

    def test_detect_type_upload_goes_to_the_inbox_root_and_jobs_back_up_first(self):
        s, r = self.upload("p.png", "auto", "")
        self.assertEqual(r["path"], "p.png")
        s, r = self.upload("p2.png", "auto", "Bank Exam", size=(910, 600))
        self.assertEqual(r["path"], "Bank Exam/p2.png")
        self.fake.detect = {"kind": "screenshot", "rotate_ccw_degrees": "0"}
        self.assertEqual(self.req("/api/run", {"profile": "nonsense"})[0], 400)
        self.req("/api/run", {"profile": ""})
        self.assertEqual(self.wait_job()["status"], "done")
        inbox = {f["name"]: f for f in self.req("/api/inbox")[1]}
        self.assertEqual(inbox["p.png"]["profile"], "auto → screenshot")
        # a second run with cards in the database makes an automatic backup first
        self.upload("p3.png", "auto", "", size=(920, 600))
        self.req("/api/run", {})
        self.wait_job()
        self.assertIn("Automatic backup saved", "\n".join(self.req("/api/job?since=0")[1]["lines"]))
        self.assertTrue(any(b["name"].startswith("auto_") for b in self.req("/api/backups")[1]))

    def test_backup_and_proofread_settings_and_the_proofread_job(self):
        self.assertEqual(self.req("/api/settings", {"backup": {"keep": 0}})[0], 400)
        self.assertEqual(self.req("/api/settings", {"backup": {"auto": False, "keep": 4}, "checks": {"proofread": False}})[0], 200)
        cfg = self.req("/api/settings")[1]
        self.assertEqual((cfg["backup"], cfg["checks"]["proofread"]), ({"auto": False, "keep": 4}, False))
        self.assertNotIn("auto", cfg["profiles"])                                     # nothing to turn for the detect type
        st = self.req("/api/status")[1]
        self.assertEqual((st["mode"]["auto_backup"], st["mode"]["proofread"]), (False, False))
        self.seed()
        self.req("/api/proofread", {})
        j = self.wait_job()
        self.assertEqual(j["status"], "done")
        self.assertIn("page(s) checked", "\n".join(self.req("/api/job?since=0")[1]["lines"]))

    def test_naming_an_unknown_exam_and_quit(self):
        items = self.seed()
        qid = items[0]["id"]
        d = self.req(f"/api/question/{qid}")[1]
        self.assertEqual(d["exam_name"], "BCS 45")                                        # folder name used
        self.assertEqual(self.req("/api/exam/new", {"question_id": qid, "label": "  "})[0], 400)
        self.assertEqual(self.req("/api/quit", {})[1]["ok"], True)


if __name__ == "__main__":
    unittest.main()

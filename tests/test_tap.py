"""Tap to answer: the script inside the cards, checked on cards rendered by Anki's own engine in a real browser.

Anki desktop is imitated as it works: one page, the answer replaces the question and scripts run again, and the card's
pycmd("ans") turns it over. AnkiDroid loads each side as a new page and turns over through window.showAnswer().
"""
import os
import re
import tempfile
import unittest

from helpers import tmpdir
from test_export import Base, Collection
from qb2anki import export_anki

try:
    from anki.collection import CsvMetadata, ImportCsvRequest
    from playwright.sync_api import sync_playwright
except Exception:                                                    # noqa: BLE001 - optional test dependencies
    sync_playwright = None

DESKTOP = """<!doctype html><html><body class="card"><div id="qa"></div><script>
function setQA(html){var qa=document.getElementById('qa');qa.innerHTML=html;
  qa.querySelectorAll('script').forEach(function(o){var s=document.createElement('script');s.textContent=o.textContent;o.replaceWith(s);});}
window.flips=0;window.pycmd=function(c){if(c==='ans'){window.flips++;setQA(window.ANSWER);}};
</script></body></html>"""


class TapMarkup(Base):
    def test_script_only_when_on_and_safe_inside_the_card(self):
        self.add(1, "Q </script> one?")
        _, rows = self.rows(export_anki.export(self.cfg, self.db)["path"])
        front, back = rows[0][3], rows[0][4]
        script = re.search(r"<script>(.*)</script>", front, re.S).group(1)
        self.assertNotIn("</", script)                              # nothing in the data can end the script early
        self.assertIn('data-l="খ"', front)
        self.assertIn('data-id="', back)
        self.cfg.raw["anki"]["tap_to_answer"] = False
        _, rows = self.rows(export_anki.export(self.cfg, self.db, include_all=True)["path"])
        self.assertNotIn("<script>", rows[0][3])
        self.assertNotIn("data-id", rows[0][4])


@unittest.skipIf(Collection is None or sync_playwright is None, "needs the anki library and playwright")
class TapInBrowser(Base):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def cards(self):
        """Two cards rendered by Anki: (question html, answer html) each."""
        self.add(1, "Capital of Bangladesh?", opts=("Khulna", "Dhaka", "Sylhet", "Rajshahi"), correct="খ")
        self.add(2, "Second question?", correct="গ")
        path = export_anki.export(self.cfg, self.db)["path"]
        col = Collection(os.path.join(tempfile.mkdtemp(), "collection.anki2"))
        self.addCleanup(col.close)
        meta = col.get_csv_metadata(str(path), None)
        col.import_csv(ImportCsvRequest(path=str(path), metadata=meta))
        out = []
        for nid in sorted(col.find_notes("")):
            card = col.get_note(nid).cards()[0]
            out.append((card.question(), card.answer()))
        return out

    def state(self, page):
        return page.evaluate("""() => ({
            ok: [...document.querySelectorAll('.qb-opt.qb-ok')].map(e => e.dataset.l),
            bad: [...document.querySelectorAll('.qb-opt.qb-bad')].map(e => e.dataset.l),
            ans: [...document.querySelectorAll('.qb-ans')].map(e => getComputedStyle(e).display),
            exp: !!document.querySelector('.qb-exp'),
            okColor: (document.querySelector('.qb-opt.qb-ok') || document.body).style && (document.querySelector('.qb-opt.qb-ok') ? getComputedStyle(document.querySelector('.qb-opt.qb-ok')).borderTopColor : null)})""")

    def desktop(self, q, a):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content(DESKTOP)
        page.evaluate("([q, a]) => { window.ANSWER = a; setQA(q); }", [q, a])
        return page

    def test_desktop_wrong_tap_turns_over_red_and_green_with_explanation(self):
        (q, a), _ = self.cards()
        page = self.desktop(q, a)
        self.assertEqual(self.state(page)["ok"], [])                 # nothing coloured before a tap
        page.click('.qb-opt[data-l="ক"]')
        page.wait_for_function("window.flips === 1")
        page.wait_for_selector(".qb-exp")
        s = self.state(page)
        self.assertEqual((s["ok"], s["bad"], s["exp"]), (["খ"], ["ক"], True))
        self.assertEqual(s["ans"], ["none"])                         # the green option replaces the answer box
        self.assertEqual(s["okColor"], "rgb(31, 157, 98)")
        page.click('.qb-opt[data-l="গ"]')                            # the back does not react to taps
        self.assertEqual(page.evaluate("window.flips"), 1)

    def test_desktop_right_tap_and_show_answer_without_tap(self):
        (q, a), (q2, a2) = self.cards()
        page = self.desktop(q, a)
        page.click('.qb-opt[data-l="খ"]')
        page.wait_for_function("window.flips === 1")
        s = self.state(page)
        self.assertEqual((s["ok"], s["bad"]), (["খ"], []))
        # next card: Show Answer pressed without tapping -> only the right option is green, no red left over
        page.evaluate("([q, a]) => { window.ANSWER = a; setQA(q); }", [q2, a2])
        page.evaluate("pycmd('ans')")
        s = self.state(page)
        self.assertEqual((s["ok"], s["bad"]), (["গ"], []))

    def test_ankidroid_new_page_per_side(self):
        (q, a), _ = self.cards()
        page = self.browser.new_page()
        self.addCleanup(page.close)
        sides = {"front": q, "back": a}
        page.route("https://card.local/*", lambda r: r.fulfill(content_type="text/html; charset=utf-8",
                   body="<!doctype html><html><body class='card'>" + sides[r.request.url.rsplit('/', 1)[1]] +
                        "<script>window.showAnswer=function(){location.href='https://card.local/back'}</script></body></html>"))
        page.goto("https://card.local/front")
        # AnkiDroid defines showAnswer before the card's script runs; the stub above is added after it, which is
        # fine because the call only happens on tap
        with page.expect_navigation():
            page.click('.qb-opt[data-l="ঘ"]')
        page.wait_for_selector(".qb-exp")
        s = self.state(page)
        self.assertEqual((s["ok"], s["bad"], s["exp"]), (["খ"], ["ঘ"], True))


if __name__ == "__main__":
    unittest.main()

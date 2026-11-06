"""The Gemini client: retries, quota handling, fallback, request settings, usage counting, key rotation."""
from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace

from helpers import make_cfg, tmpdir
from PIL import Image

import qb2anki.gemini_client as gc
from qb2anki.db import DB, quota_day
from qb2anki.errors import KeysFlagged, NoQuotaForModel, QuotaExhausted
from qb2anki.gemini_client import GeminiClient, GeminiError
from qb2anki.keys import KeyPool, parse_keys

try:
    from google.genai import errors as gerr
except ImportError:  # pragma: no cover
    gerr = None


def fake_key(n: int) -> str:
    return "AIza" + f"{n:02d}" + "x" * 30


def resp(text='{"ok": true}', usage=None):
    return SimpleNamespace(text=text, usage_metadata=usage)


def sdk(script):
    """A fake SDK client whose generate_content pops from `script` (exceptions are raised)."""
    class Models:
        def generate_content(self, model, contents, config):
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return resp(step) if isinstance(step, str) else step
    return SimpleNamespace(models=Models())


class ClientBase(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.cfg = make_cfg(self.root)
        self.cfg.raw["gemini"].update({"request_delay_sec": 0, "max_retries": 3, "requests_per_minute": 0, "fallback_models": [],
                                       "key_daily_cap": 3, "halt_after_blocked": 2})
        # a fake clock: sleeping advances time instantly
        self.sleeps, self.now = [], [1000.0]
        orig = (gc.time.sleep, gc.time.monotonic)
        gc.time.sleep = lambda x: (self.sleeps.append(x), self.now.__setitem__(0, self.now[0] + x))
        gc.time.monotonic = lambda: self.now[0]
        self.addCleanup(lambda: (setattr(gc.time, "sleep", orig[0]), setattr(gc.time, "monotonic", orig[1])))

    def client(self, script=None, use_cache=False, db=None):
        c = GeminiClient(self.cfg, use_cache=use_cache, db=db)
        if script is not None:
            c._client = sdk(script)
        return c

    def call(self, c, model="gemini-3.6-flash", prompt="p", image=None, **kw):
        return c.generate_json(model=model, prompt=prompt, schema={"type": "OBJECT"}, image=image, label="t", **kw)


@unittest.skipIf(gerr is None, "google-genai missing")
class Retries(ClientBase):
    def test_busy_errors_are_retried_and_fenced_json_is_parsed(self):
        c = self.client([gerr.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}), '```json\n{"ok": true}\n```'])
        self.assertEqual(self.call(c), {"ok": True})

    def test_broken_json_and_per_minute_limits_are_retried(self):
        c = self.client(['{"ok": tru', gerr.ClientError(429, {"error": {"message": "limit per minute", "status": "RESOURCE_EXHAUSTED"}}), '{"ok": 1}'])
        self.assertEqual(self.call(c), {"ok": 1})

    def test_fatal_errors(self):
        for err, cls in ((gerr.ClientError(404, {"error": {"message": "models/zzz is not found", "status": "NOT_FOUND"}}), GeminiError),
                         (gerr.ClientError(429, {"error": {"message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "status": "RESOURCE_EXHAUSTED"}}), QuotaExhausted),
                         (gerr.ClientError(429, {"error": {"message": "Quota ... limit: 0", "status": "RESOURCE_EXHAUSTED"}}), NoQuotaForModel)):
            script = [err, '{"x":1}']
            with self.assertRaises(cls):
                self.call(self.client(script))
            self.assertEqual(len(script), 1)                     # never retried

    def test_missing_key_message(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(GeminiError) as cm:
            self.call(self.client())
        self.assertIn("No API key", str(cm.exception))

    def test_cache_makes_repeats_free_and_pacing_waits(self):
        script = ['{"a": 1}']
        c = self.client(script, use_cache=True)
        self.assertEqual(self.call(c), {"a": 1})
        self.assertEqual(self.call(c), {"a": 1})                 # second time: cache, script is empty
        self.assertEqual(c.cache_hits, 1)
        self.cfg.raw["gemini"]["requests_per_minute"] = 2
        c2 = self.client(['{"a":1}'] * 3)
        for i in range(3):
            self.call(c2, prompt=f"p{i}")
        self.assertTrue(any(w > 30 for w in self.sleeps))

    def test_sdk_client_is_created_once_with_many_threads(self):
        import google.genai as genai
        made, real = [], genai.Client

        class Counting:
            def __init__(self, api_key=None):
                import time
                time.sleep(0.01)
                made.append(self)
        genai.Client = Counting
        self.addCleanup(setattr, genai, "Client", real)
        os.environ["GEMINI_API_KEY"] = "x"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        c = self.client()
        ts = [threading.Thread(target=c._api) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(made), 1)


@unittest.skipIf(gerr is None, "google-genai missing")
class FallbackAndSettings(ClientBase):
    def test_falls_back_to_another_model_and_reuses_the_cache(self):
        self.cfg.raw["gemini"]["fallback_models"] = ["model-b"]
        used = []
        daily = gerr.ClientError(429, {"error": {"message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "status": "RESOURCE_EXHAUSTED"}})

        class Models:
            primary_ok = False

            def generate_content(s, model, contents, config):
                used.append(model)
                if model == "model-a" and not s.primary_ok:
                    raise daily
                return resp('{"from": "%s"}' % model)
        models = Models()
        c = self.client(use_cache=True)
        c._client = SimpleNamespace(models=models)
        self.assertEqual(self.call(c, model="model-a"), {"from": "model-b"})
        models.primary_ok, n = True, len(used)
        self.assertEqual(self.call(c, model="model-a"), {"from": "model-b"})      # answer already cached: no request
        self.assertEqual(len(used), n)
        models.primary_ok = False
        with self.assertRaises(QuotaExhausted):
            self.call(c, model="model-a", prompt="other", allow_fallback=False)

    def test_gemini3_gets_no_temperature_and_high_resolution_and_thinking_level(self):
        seen = {}

        class Models:
            def generate_content(s, model, contents, config):
                seen["config"] = config
                return resp()
        self.cfg.raw["gemini"].update({"temperature": 0.0, "thinking_level": "low"})
        c = self.client()
        c._client = SimpleNamespace(models=Models())
        self.call(c, image=Image.new("RGB", (4, 4)))
        conf = seen["config"]
        self.assertIsNone(conf.temperature)
        self.assertEqual(str(conf.media_resolution.value), "MEDIA_RESOLUTION_HIGH")
        self.assertEqual(conf.thinking_config.thinking_level.value, "LOW")
        self.call(c, model="gemini-2.5-pro", prompt="old")
        self.assertEqual(seen["config"].temperature, 0.0)

    def test_usage_is_counted_per_model_and_saved_once(self):
        from qb2anki.pipeline import Pipeline
        db = DB(self.cfg.db_path)
        self.addCleanup(db.close)
        usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=50, thoughts_token_count=20)
        c = self.client([resp(usage=usage), resp(usage=usage), resp(usage=usage)])
        for i, m in enumerate(("model-a", "model-a", "model-b")):
            self.call(c, model=m, prompt=f"p{i}")
        pipe = Pipeline(self.cfg, db, c, say=lambda *_: None)
        pipe.flush_usage()
        pipe.flush_usage()
        rows = {r["model"]: r for r in db.usage_for(quota_day())}
        self.assertEqual((rows["model-a"]["calls"], rows["model-a"]["input_tokens"], rows["model-a"]["output_tokens"]), (2, 200, 140))
        self.assertEqual(rows["model-b"]["calls"], 1)


class Pool(unittest.TestCase):
    def test_parse_keys(self):
        txt = f"# comment\n\n{fake_key(1)}\nlabel = {fake_key(2)}\n{fake_key(1)}\nsomething,{fake_key(3)}\n"
        self.assertEqual(parse_keys(txt), [fake_key(1), fake_key(2), fake_key(3)])

    def test_spreads_evenly_respects_cap_and_is_per_model(self):
        pool = KeyPool([fake_key(i) for i in range(4)], cap=2)
        used = []
        for _ in range(8):
            k = pool.acquire("m")
            pool.record_success(k, "m")
            used.append(k.label)
        self.assertEqual(sorted(used), ["k001", "k001", "k002", "k002", "k003", "k003", "k004", "k004"])
        with self.assertRaises(QuotaExhausted):
            pool.acquire("m")
        self.assertEqual(pool.acquire("other").label, "k001")

    def test_state_persists_and_two_blocked_keys_halt(self):
        db = DB(make_cfg(tmpdir(self)).db_path)
        self.addCleanup(db.close)
        pool = KeyPool([fake_key(i) for i in range(5)], cap=5, halt_after_blocked=2)
        pool.record_success(pool.acquire("m"), "m")
        pool.on_quota_exhausted(pool.keys[1], "m")
        pool.on_key_rejected(pool.keys[0], "blocked", "denied")
        with self.assertRaises(KeysFlagged):
            pool.on_key_rejected(pool.keys[2], "blocked", "denied")
        pool.flush(db)
        again = KeyPool([fake_key(i) for i in range(5)], cap=5, halt_after_blocked=2, usage=db.key_usage_today(quota_day()), states=db.key_states())
        with self.assertRaises(KeysFlagged):
            again.acquire("m")
        db.clear_key_states()
        again2 = KeyPool([fake_key(i) for i in range(5)], cap=5, usage=db.key_usage_today(quota_day()), states=db.key_states())
        self.assertIn((pool.keys[1].id, "m"), again2.exhausted)
        self.assertEqual(again2.calls[(pool.keys[0].id, "m")], 1)
        self.assertNotIn("xxxx", repr(pool.keys[0]) + str(pool.keys[0]))


@unittest.skipIf(gerr is None, "google-genai missing")
class Rotation(ClientBase):
    def rot_client(self, behaviours):
        db = DB(self.cfg.db_path)
        self.addCleanup(db.close)
        (self.root / "keys.txt").write_text("\n".join(fake_key(i) for i in range(len(behaviours))), encoding="utf-8")
        c = self.client(db=db)
        self.assertIsNotNone(c.pool)

        def make(beh):
            class Models:
                def generate_content(s, model, contents, config):
                    if isinstance(beh, Exception):
                        raise beh
                    if isinstance(beh, dict) and model in beh:
                        raise beh[model]
                    return resp()
            return SimpleNamespace(models=Models())
        stubs = {f"k{i + 1:03d}": make(b) for i, b in enumerate(behaviours)}
        c._client_factory = lambda key: stubs[key.label]
        return c

    def test_moves_on_when_a_key_is_used_up_blocked_or_invalid_and_spreads_load(self):
        daily = gerr.ClientError(429, {"error": {"message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "status": "RESOURCE_EXHAUSTED"}})
        denied = gerr.ClientError(403, {"error": {"message": "Permission denied", "status": "PERMISSION_DENIED"}})
        bad = gerr.ClientError(400, {"error": {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}})
        c = self.rot_client([daily, denied, bad, None, None])
        self.assertEqual(self.call(c), {"ok": True})
        p = c.pool
        self.assertIn((p.keys[0].id, "gemini-3.6-flash"), p.exhausted)
        self.assertEqual(p.state[p.keys[1].id][0], "blocked")
        self.assertEqual(p.state[p.keys[2].id][0], "bad")
        self.call(c, prompt="second")
        self.assertEqual((p.calls[(p.keys[3].id, "gemini-3.6-flash")], p.calls[(p.keys[4].id, "gemini-3.6-flash")]), (1, 1))

    def test_second_blocked_key_stops_the_run(self):
        denied = gerr.ClientError(403, {"error": {"message": "Permission denied", "status": "PERMISSION_DENIED"}})
        c = self.rot_client([denied, denied, None])
        with self.assertRaises(KeysFlagged) as cm:
            self.call(c)
        self.assertIn("STOPPED", str(cm.exception))

    def test_limit_zero_on_two_keys_closes_only_that_model(self):
        zero = gerr.ClientError(429, {"error": {"message": "Quota ... limit: 0", "status": "RESOURCE_EXHAUSTED"}})
        c = self.rot_client([{"pro": zero}, {"pro": zero}, None, None])
        with self.assertRaises(NoQuotaForModel):
            self.call(c, model="pro")
        self.assertEqual(self.call(c, model="flash", prompt="other"), {"ok": True})


if __name__ == "__main__":
    unittest.main()

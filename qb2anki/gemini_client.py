"""Thin wrapper around the google-genai SDK.

* every call asks for JSON that matches a schema
* answers are cached on disk (work/cache), so re-runs and code fixes do not cost API calls
* transient errors (429/5xx/bad JSON) are retried with backoff
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from PIL import Image

from . import prompts
from .errors import (GeminiError, KeyBlocked, KeyRejected, NoQuotaForModel,
                     QuotaExhausted)

log = logging.getLogger("qb2anki.gemini")


def to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def _short(msg: str, limit: int = 200) -> str:
    """The human part of a Google API error (they arrive as a noisy dict dump)."""
    m = re.search(r"'message': '(.*?)'(?:, 'status'|\})", msg, re.S)
    return (m.group(1) if m else msg).strip()[:limit]


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


class GeminiClient:
    def __init__(self, cfg, use_cache: bool = True, db=None):
        self.cfg = cfg
        self.use_cache = use_cache
        self._client = None
        self._clients: dict = {}                  # key id -> SDK client (only when rotating over several keys)
        self._client_factory = None               # test hook: key -> client
        self.pool = None
        if db is not None:                        # optional key rotation: needs keys.txt with two or more keys
            from .keys import KeyPool
            self.pool = KeyPool.from_config(cfg, db)
        self.api_calls = 0
        self.cache_hits = 0
        self.usage_log: list[tuple[str, int, int]] = []     # (model, input tokens, output tokens incl. thinking)
        self._usage_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._rl_lock = threading.Lock()
        self._rl: dict[str, deque] = defaultdict(deque)

    # ------------------------------------------------------------------ SDK access
    def _api(self):
        # Several worker threads call this at once. Creating the SDK client twice made one thread use a client that
        # was then closed ("Cannot send a request, as the client has been closed"), so create it exactly once.
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    try:
                        from google import genai
                    except ImportError as e:  # pragma: no cover
                        raise GeminiError("google-genai is not installed. Run: pip install -r requirements.txt") from e
                    env = self.cfg.get("gemini", "api_key_env", "GEMINI_API_KEY")
                    key = os.environ.get(env) or self._first_key_from_file()
                    if not key:
                        raise GeminiError(
                            f"No API key found. Set the {env} environment variable or put {env}=... in the .env file."
                        )
                    self._client = genai.Client(api_key=key)
        return self._client

    def _first_key_from_file(self):
        from .keys import parse_keys
        path = self.cfg.root / self.cfg.get("gemini", "keys_file", "keys.txt")
        if path.exists():
            ks = parse_keys(path.read_text(encoding="utf-8", errors="ignore"))
            return ks[0] if ks else None
        return None

    def _client_for(self, key):
        """SDK client for a pool key (created once per key); the single-key client when not rotating."""
        if key is None:
            return self._api()
        if self._client_factory is not None:
            return self._client_factory(key)
        with self._init_lock:
            if key.id not in self._clients:
                from google import genai
                self._clients[key.id] = genai.Client(api_key=key.secret)
            return self._clients[key.id]

    def _acquire_key(self, model: str):
        """None without a pool. With one: the least-used usable key; waits out a per-minute cool-down if that is all that's left."""
        if self.pool is None:
            return None
        while True:
            try:
                return self.pool.acquire(model)
            except QuotaExhausted:
                wait = self.pool.cooldown_wait(model)
                if wait is None:
                    raise
                time.sleep(min(wait + 0.5, 70.0))

    def warm_up(self) -> None:
        """Create the SDK client in the main thread before any worker threads start."""
        try:
            self._api()
        except GeminiError:
            pass   # e.g. no key while everything is cached; a real request will raise the proper error

    def check_key(self, secret: str, model: str) -> tuple[str, str]:
        """One tiny request with this key -> (status, note). status: ok | bad | blocked | no_quota | used_up | busy | error."""
        from google import genai
        from google.genai import types
        try:
            genai.Client(api_key=secret).models.generate_content(
                model=model, contents='Reply with {"ok": true}',
                config=types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=200))
            return "ok", ""
        except Exception as e:  # noqa: BLE001
            retryable, fatal = self._classify(e)
            if fatal:
                cls = fatal[0]
                kind = ("blocked" if issubclass(cls, KeyBlocked) else "bad" if issubclass(cls, KeyRejected)
                        else "no_quota" if issubclass(cls, NoQuotaForModel)
                        else "used_up" if issubclass(cls, QuotaExhausted) else "error")
                return kind, _short(fatal[1], 120)
            return "busy", _short(str(e), 120)

    def list_models(self) -> list[str]:
        names = []
        for m in self._api().models.list():
            names.append(getattr(m, "name", str(m)).replace("models/", ""))
        return sorted(names)

    # ------------------------------------------------------------------ cache
    def _settings_fingerprint(self) -> str:
        g = self.cfg.raw.get("gemini", {})
        return json.dumps({k: g.get(k) for k in ("temperature", "thinking_level", "thinking_budget",
                                                   "media_resolution", "max_output_tokens")}, sort_keys=True)

    def _cache_key(self, model, prompt, schema, image_bytes, system) -> str:
        h = hashlib.sha256()
        for part in (model, prompts.PROMPT_VERSION, system or "", prompt, json.dumps(schema, sort_keys=True),
                     self._settings_fingerprint()):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        h.update(image_bytes or b"")
        return h.hexdigest()[:40]

    def _cache_path(self, key: str) -> Path:
        return self.cfg.cache_dir / f"{key}.json"

    # ------------------------------------------------------------------ main entry
    def generate_json(self, *, model: str, prompt: str, schema: dict, image: Image.Image | None = None,
                      system: str | None = prompts.SYSTEM, label: str = "", allow_fallback: bool = True) -> dict:
        """Ask `model`; if it is out of quota or overloaded, try the `fallback_models` (each model has its own quota).

        A cached answer from ANY model in the list is reused first, so quota is never spent twice on the same question.
        """
        models = [model]
        if allow_fallback:
            models += [m for m in (self.cfg.get("gemini", "fallback_models", []) or []) if m != model]
        if self.use_cache:
            for m in models:
                hit = self._cached(m, prompt, schema, image, system)
                if hit is not None:
                    self.cache_hits += 1
                    return hit
        last: Exception | None = None
        for i, m in enumerate(models):
            try:
                return self._generate_one(model=m, prompt=prompt, schema=schema, image=image, system=system, label=label)
            except (QuotaExhausted, NoQuotaForModel) as e:
                last = e
            except GeminiError as e:
                if not any(w in str(e).lower() for w in ("high demand", "unavailable", "overloaded", "failed after")):
                    raise
                last = e
            if i + 1 < len(models):
                log.warning("[%s] %s unavailable (%s); trying %s", label, m, _short(str(last), 90), models[i + 1])
        raise last  # type: ignore[misc]

    def _cached(self, model, prompt, schema, image, system):
        quality = int(self.cfg.get("image", "jpeg_quality", 92))
        image_bytes = to_jpeg_bytes(image, quality) if image is not None else None
        path = self._cache_path(self._cache_key(model, prompt, schema, image_bytes, system))
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))["result"]
            except Exception:  # noqa: BLE001
                return None
        return None

    def _generate_one(self, *, model: str, prompt: str, schema: dict, image: Image.Image | None,
                      system: str | None, label: str) -> dict:
        quality = int(self.cfg.get("image", "jpeg_quality", 92))
        image_bytes = to_jpeg_bytes(image, quality) if image is not None else None
        key = self._cache_key(model, prompt, schema, image_bytes, system)
        path = self._cache_path(key)
        if self.use_cache and path.exists():
            try:
                self.cache_hits += 1
                return json.loads(path.read_text(encoding="utf-8"))["result"]
            except Exception:  # corrupt cache entry -> redo
                log.warning("corrupt cache entry %s, redoing", path.name)

        result, raw_text = self._call_with_retries(model, prompt, schema, image_bytes, system, label)

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"label": label, "model": model, "prompt_version": prompts.PROMPT_VERSION,
                   "raw_text": raw_text, "result": result}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return result

    # ------------------------------------------------------------------ internals
    def _throttle(self, model: str, bucket: str = "") -> None:
        """Stay under `requests_per_minute` per model (free-tier limits are per model and per project)."""
        rpm = int(self.cfg.get("gemini", "requests_per_minute", 8))
        if rpm <= 0:
            return
        while True:
            with self._rl_lock:
                now = time.monotonic()
                dq = self._rl[f"{model}|{bucket}"]
                while dq and now - dq[0] > 60:
                    dq.popleft()
                if len(dq) < rpm:
                    dq.append(now)
                    return
                wait = 60 - (now - dq[0]) + 0.05
            time.sleep(wait)

    def _pool_handles(self, key, model: str, exc_cls, e: Exception) -> bool:
        """Record what Google said about this key. True = a different key should be tried right away."""
        pool = self.pool
        if issubclass(exc_cls, KeyBlocked):
            return pool.on_key_rejected(key, "blocked", str(e))
        if issubclass(exc_cls, KeyRejected):
            return pool.on_key_rejected(key, "bad", str(e))
        if issubclass(exc_cls, NoQuotaForModel):
            return pool.on_limit_zero(key, model)
        if issubclass(exc_cls, QuotaExhausted):
            return pool.on_quota_exhausted(key, model)
        return False

    def _call_with_retries(self, model, prompt, schema, image_bytes, system, label):
        max_retries = int(self.cfg.get("gemini", "max_retries", 6))
        max_switches = (len(self.pool.keys) * 2 + 2) if self.pool else 0
        last: Exception | None = None
        attempt = attempts = switches = 0
        while attempt <= max_retries:
            attempts = attempt + 1
            key = None
            try:
                key = self._acquire_key(model)
                text = self._call_once(model, prompt, schema, image_bytes, system, key)
                result = json.loads(_strip_fences(text))
                if key is not None:
                    self.pool.record_success(key, model)
                time.sleep(float(self.cfg.get("gemini", "request_delay_sec", 0.5)))
                return result, text
            except GeminiError:
                if key is not None:
                    self.pool.release(key, model)
                raise
            except Exception as e:  # noqa: BLE001
                if key is not None:
                    self.pool.release(key, model)
                last = e
                retryable, fatal = self._classify(e)
                if fatal:
                    exc_cls, fatal_msg = fatal
                    if key is not None and switches < max_switches and self._pool_handles(key, model, exc_cls, e):
                        switches += 1
                        continue                       # another key may still work: no waiting, no attempt used
                    raise exc_cls(f"[{label or model}] {fatal_msg}") from e
                rate_limited = getattr(e, "code", None) == 429 or "resource_exhausted" in str(e).lower()
                if key is not None and rate_limited and switches < max_switches and self.pool.on_cooldown(key, model):
                    switches += 1
                    continue                           # that key's per-minute limit is hit; use another
                if not retryable or attempt == max_retries:
                    break
                if "client has been closed" in str(e).lower():
                    with self._init_lock:
                        self._client = None            # rebuilt on the next _api() call
                        self._clients.clear()
                wait = min(60.0, (2 ** attempt) + random.random() * 2)
                log.warning("[%s] attempt %d failed (%s); retrying in %.1fs", label or model, attempt + 1,
                            str(e)[:160], wait)
                time.sleep(wait)
                attempt += 1
        raise GeminiError(f"[{label or model}] failed after {attempts} attempt(s): {last}")

    @staticmethod
    def _classify(e: Exception):
        """-> (retryable, fatal) where fatal is None or (ExceptionClass, message)."""
        msg = str(e)
        if isinstance(e, ImportError):
            return False, (GeminiError, f"a required package is broken ({msg}). Run doctor.bat for the fix.")
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        low = msg.lower()
        squashed = re.sub(r"[\s_\-]", "", low)
        if isinstance(e, json.JSONDecodeError):
            return True, None  # model returned broken JSON: try again
        if "client has been closed" in low:
            return True, None  # handled in _call_with_retries by rebuilding the client
        if code == 404 or ("not found" in low and "model" in low) or "no longer available" in low:
            return False, (GeminiError, "Model not found or retired. Run `python -m qb2anki models --test` and fix "
                                        f"config.toml. ({_short(msg)})")
        if code in (400, 401) and any(w in low for w in ("api key not valid", "api_key_invalid", "api key expired", "key expired")):
            return False, (KeyRejected, f"API key rejected ({code}): {_short(msg, 200)}")
        if code == 401:
            return False, (KeyRejected, f"API key rejected ({code}): {_short(msg, 200)}")
        if code == 403 or "permission_denied" in low:
            return False, (KeyBlocked, f"Google refused this key's project ({code}): {_short(msg, 240)}")
        if code in (400, 401, 403):
            return False, (GeminiError, f"API rejected the request ({code}): {_short(msg, 300)}")
        if code == 429 or "resource_exhausted" in low or "quota" in low:
            if "limit: 0" in low:
                return False, (NoQuotaForModel, "Your project has NO quota for this model (limit 0). This is typical for "
                                                "Pro models on the free tier. Pick another model: python -m qb2anki models --test")
            if "perday" in squashed or "daily" in low:
                return False, (QuotaExhausted, f"Daily quota used up for this model: {_short(msg)}")
            return True, None
        if code in (500, 502, 503, 504) or any(w in low for w in ("unavailable", "overloaded", "deadline", "timeout",
                                                                  "timed out", "connection", "empty response",
                                                                  "high demand")):
            return True, None
        return False, None

    def _call_once(self, model, prompt, schema, image_bytes, system, key=None) -> str:
        from google.genai import types

        self._throttle(model, key.id if key is not None else "")
        self.api_calls += 1
        cfg = self.cfg
        gen = dict(
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=int(cfg.get("gemini", "max_output_tokens", 32000)),
        )
        # Google: for Gemini 3.x keep temperature at its default; lowering it can cause looping/degraded output.
        temp = cfg.get("gemini", "temperature")
        if temp is not None and not model.lower().startswith(("gemini-3", "gemini-4")):
            gen["temperature"] = float(temp)
        if system:
            gen["system_instruction"] = system
        tl = cfg.get("gemini", "thinking_level")
        tb = cfg.get("gemini", "thinking_budget")
        if tl:
            gen["thinking_config"] = types.ThinkingConfig(thinking_level=str(tl).upper())
        elif tb is not None:
            gen["thinking_config"] = types.ThinkingConfig(thinking_budget=int(tb))
        mr = cfg.get("gemini", "media_resolution")
        if mr:
            enum = getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{str(mr).upper()}", None)
            if enum is not None:
                gen["media_resolution"] = enum

        contents = []
        if image_bytes is not None:
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        contents.append(prompt)

        resp = self._client_for(key).models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(**gen)
        )
        um = getattr(resp, "usage_metadata", None)
        t_in = int(getattr(um, "prompt_token_count", 0) or 0)
        t_out = int(getattr(um, "candidates_token_count", 0) or 0) + int(getattr(um, "thoughts_token_count", 0) or 0)
        with self._usage_lock:
            self.usage_log.append((model, t_in, t_out))
        text = getattr(resp, "text", None)
        if not text:
            reason = None
            try:
                reason = resp.candidates[0].finish_reason
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"empty response (finish_reason={reason})")
        return text

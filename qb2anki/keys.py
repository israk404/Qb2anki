"""Optional rotation over several API keys.

Google applies free-tier limits per PROJECT and per model, so each key (= its own project) has its own allowance. With a
list of keys in keys.txt the tool spreads its requests over them: every request goes to the key that has been used least today
for that model. Keys that Google rejects are retired, and the whole rotation HALTS if Google blocks two keys.

Note: Google's terms do not allow using several projects to get around a quota, and projects have been restricted for it.
This is opt-in, and entirely at your own risk (see README).
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from .db import quota_day
from .errors import KeysFlagged, NoQuotaForModel, QuotaExhausted

_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


@dataclass(frozen=True)
class Key:
    id: str       # short fingerprint, safe to show in logs and the database
    label: str    # k001, k002, ... (position in keys.txt)
    secret: str

    def __repr__(self) -> str:      # never print the secret by accident
        return f"Key({self.label})"

    __str__ = __repr__


def parse_keys(text: str) -> list[str]:
    """One key per line. Blank lines and lines starting with # are ignored; 'name,key' / 'name key' / 'name=key' work too."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = _KEY_RE.findall(line)
        if not found:
            tokens = [t for t in re.split(r"[\s,=:;]+", line) if len(t) >= 20]
            found = tokens[-1:]
        for k in found:
            if k not in out:
                out.append(k)
    return out


def _fp(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:10]


class KeyPool:
    def __init__(self, secrets: list[str], cap: int = 12, halt_after_blocked: int = 2, usage: list[dict] | None = None,
                 states: dict | None = None, clock=time.monotonic):
        self.keys = [Key(_fp(s), f"k{i + 1:03d}", s) for i, s in enumerate(dict.fromkeys(secrets))]
        self.cap, self.halt_after_blocked, self.clock = int(cap), int(halt_after_blocked), clock
        self._lock = threading.RLock()
        self.calls: dict = defaultdict(int)          # (key id, model) -> successful requests today
        self.exhausted: set = set()                  # (key id, model) used up today
        self.state: dict = dict(states or {})        # key id -> (kind 'bad'|'blocked', note)
        self.cool: dict = {}                         # (key id, model) -> usable again at this clock time
        self.limit_zero: dict = defaultdict(set)     # model -> key ids that reported "limit 0"
        self.dead_models: set = set()
        self._dirty_calls: dict = defaultdict(int)
        self._dirty_exh: set = set()
        self._dirty_state: dict = {}
        for r in usage or []:
            self.calls[(r["key_id"], r["model"])] = int(r["calls"])
            if r["exhausted"]:
                self.exhausted.add((r["key_id"], r["model"]))

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, cfg, db):
        """-> KeyPool, or None when no list of (at least two) keys is configured."""
        import os
        path = cfg.root / cfg.get("gemini", "keys_file", "keys.txt")
        if not path.exists():
            return None
        secrets = parse_keys(path.read_text(encoding="utf-8", errors="ignore"))
        env_key = os.environ.get(cfg.get("gemini", "api_key_env", "GEMINI_API_KEY"))
        if env_key and env_key not in secrets:
            secrets.insert(0, env_key)
        if len(secrets) < 2:
            return None
        day = quota_day()
        return cls(secrets, cap=cfg.get("gemini", "key_daily_cap", 12),
                   halt_after_blocked=cfg.get("gemini", "halt_after_blocked", 2),
                   usage=db.key_usage_today(day), states=db.key_states())

    # ------------------------------------------------------------------ choosing a key
    def blocked_count(self) -> int:
        return sum(1 for kind, _ in self.state.values() if kind == "blocked")

    def _usable(self, k: Key, model: str, now: float, ignore_cool: bool = False) -> bool:
        pair = (k.id, model)
        return (k.id not in self.state and pair not in self.exhausted and self.calls[pair] < self.cap
                and (ignore_cool or self.cool.get(pair, 0) <= now))

    def acquire(self, model: str) -> Key:
        with self._lock:
            if self.blocked_count() >= self.halt_after_blocked:
                raise KeysFlagged(self._flagged_message())
            if model in self.dead_models:
                raise NoQuotaForModel(f"none of your keys has any quota for {model} (limit 0)")
            now = self.clock()
            best = None
            for idx, k in enumerate(self.keys):
                if self._usable(k, model, now):
                    score = (self.calls[(k.id, model)], idx)
                    if best is None or score < best[0]:
                        best = (score, k)
            if best:
                return best[1]
            bad = sum(1 for kind, _ in self.state.values() if kind == "bad")
            raise QuotaExhausted(f"all usable keys are used up for {model} today "
                                 f"({len(self.keys)} keys, {bad} invalid, {self.blocked_count()} blocked, cap {self.cap}/key/model)")

    def cooldown_wait(self, model: str) -> float | None:
        """Seconds until a cooling-down key is usable again; None if no key is merely cooling down."""
        with self._lock:
            now = self.clock()
            waits = [self.cool[(k.id, model)] - now for k in self.keys
                     if self._usable(k, model, now, ignore_cool=True) and self.cool.get((k.id, model), 0) > now]
            return max(0.0, min(waits)) if waits else None

    def has_candidate(self, model: str) -> bool:
        with self._lock:
            now = self.clock()
            return model not in self.dead_models and any(self._usable(k, model, now, ignore_cool=True) for k in self.keys)

    # ------------------------------------------------------------------ outcomes
    def record_success(self, key: Key, model: str) -> None:
        with self._lock:
            self.calls[(key.id, model)] += 1
            self._dirty_calls[(key.id, model)] += 1

    def on_quota_exhausted(self, key: Key, model: str) -> bool:
        with self._lock:
            self.exhausted.add((key.id, model))
            self._dirty_exh.add((key.id, model))
            return self.has_candidate(model)

    def on_limit_zero(self, key: Key, model: str) -> bool:
        """True if another key is worth trying. Two keys reporting 'limit 0' means the model is closed to all of them."""
        with self._lock:
            self.exhausted.add((key.id, model))
            self._dirty_exh.add((key.id, model))
            self.limit_zero[model].add(key.id)
            if len(self.limit_zero[model]) >= 2:
                self.dead_models.add(model)
                return False
            return self.has_candidate(model)

    def on_cooldown(self, key: Key, model: str, seconds: float = 65.0) -> bool:
        with self._lock:
            self.cool[(key.id, model)] = self.clock() + seconds
            return self.has_candidate(model)

    def on_key_rejected(self, key: Key, kind: str, note: str, halt: bool = True) -> bool:
        with self._lock:
            self.state[key.id] = (kind, note[:200])
            self._dirty_state[key.id] = (kind, note[:200])
            if halt and self.blocked_count() >= self.halt_after_blocked:
                raise KeysFlagged(self._flagged_message())
            return any(k.id not in self.state for k in self.keys)

    def _flagged_message(self) -> str:
        return (f"Google blocked {self.blocked_count()} of your keys (permission denied / restricted), so key rotation is "
                f"STOPPED to leave the other keys alone. Check the Google AI Studio / Cloud console for notices. "
                f"To continue anyway later: python -m qb2anki keys --reset")

    # ------------------------------------------------------------------ persistence + reporting
    def flush(self, db) -> None:
        with self._lock:
            day = quota_day()
            for (kid, model), n in self._dirty_calls.items():
                db.add_key_usage(day, kid, model, n, (kid, model) in self._dirty_exh)
            for kid, model in self._dirty_exh:
                if (kid, model) not in self._dirty_calls:
                    db.add_key_usage(day, kid, model, 0, True)
            for kid, (kind, note) in self._dirty_state.items():
                db.set_key_state(kid, kind, note)
            self._dirty_calls.clear()
            self._dirty_exh.clear()
            self._dirty_state.clear()

    def summary(self) -> dict:
        with self._lock:
            per_key = defaultdict(int)
            for (kid, _m), n in self.calls.items():
                per_key[kid] += n
            return {
                "keys": len(self.keys),
                "bad": sum(1 for kind, _ in self.state.values() if kind == "bad"),
                "blocked": self.blocked_count(),
                "calls_today": sum(per_key.values()),
                "keys_used_today": sum(1 for v in per_key.values() if v),
                "exhausted_pairs": len(self.exhausted),
            }

"""Configuration.

Three layers, later ones win:
  1. built-in defaults (below)
  2. config.toml          (shipped with the tool; you may edit it by hand)
  3. settings.json        (written by the Settings page of the app)
plus an optional .env file that holds GEMINI_API_KEY.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        tomllib = None

DEFAULT_SUBJECTS = {
    "Bangla": "Bangla language, grammar, literature, authors, vocabulary",
    "English": "English grammar, vocabulary, idioms, translation, English and world literature",
    "Math": "Arithmetic, algebra, geometry, mental ability, any calculation-based question",
    "General Knowledge": "Bangladesh affairs, international affairs, history, geography, constitution, current affairs, organisations",
    "General Science": "Physics, chemistry, biology, general science, environment",
    "ICT": "Computers, software, internet, digital technology",
    "Nursing & Midwifery": "Nursing, midwifery, medical, clinical and public-health questions",
}

DEFAULTS: dict = {
    "paths": {"inbox": "inbox", "work": "work", "data": "data", "out": "out"},
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "layout_model": "gemini-3.6-flash",
        "extract_models": ["gemini-3.6-flash", "gemini-3.5-flash"],   # [0] reads; [1] only when cross_check is on
        "fallback_models": [],
        "fill_model": "gemini-3.6-flash",                              # AI answers/explanations and the math check
        "max_output_tokens": 32000,
        "max_retries": 6,
        "request_delay_sec": 0.5,
        "requests_per_minute": 5,
        "concurrency": 2,
        "media_resolution": "high",
        "key_mode": "single",            # "single": one key (paid or free) | "rotate": spread requests over keys.txt
        "keys_file": "keys.txt",
        "key_daily_cap": 12,
        "halt_after_blocked": 2,
    },
    "image": {"crop_pad": 0.006, "upscale": 1.0, "jpeg_quality": 92, "layout_max_width": 2000},
    "checks": {
        "proofread": True,           # AI compares each page's reading with its picture and corrects reading errors (1 request/page)
        "cross_check": False,        # read every page twice with two models and flag differences (doubles requests)
        "math_check": True,          # lint every formula; AI re-checks and repairs math explanations
        "fact_check": True,          # AI cross-checks every question, option, answer and explanation (text only, 1 request/8 cards)
        "fact_policy": "review",     # when the AI disagrees with the source: "review" | "auto" (use high-confidence fixes) | "note"
        "ai_fill": True,             # AI answers / explanations the source does not give (used when fact_check is off)
        "allowed_option_counts": [4, 5],
        "stem_similarity": 0.97,
        "option_similarity": 0.97,
        "explanation_similarity": 0.95,
        "near_duplicate_ratio": 0.92,
        "block_export_on_soft_flags": False,
    },
    "anki": {"deck_root": "Job Questions", "notetype": "Basic", "source_line": "exam", "page_tag": False},
    "ui": {"port": 8765},
    "backup": {"auto": True, "keep": 10},
    "profiles": {
        "default": "auto",
        "auto": {"detect": True},                      # AI works out the photo type and the rotation (1 small request/photo)
        "book": {"rotate": 90, "layout": True, "reader": "book"},
        "screenshot": {"rotate": 0, "layout": False, "reader": "general"},
    },
    "subjects": DEFAULT_SUBJECTS,
}

_REPLACE_WHOLE = {"subjects", "extract_models", "fallback_models", "allowed_option_counts"}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)       # never share nested dicts with DEFAULTS
    for k, v in over.items():
        if k in _REPLACE_WHOLE:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and (k not in os.environ or not os.environ[k]):
            os.environ[k] = v


class Config:
    def __init__(self, root: Path, raw: dict):
        self.root = Path(root)
        self.raw = raw

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    def _p(self, key: str) -> Path:
        return self.root / self.get("paths", key, key)

    @property
    def inbox(self) -> Path: return self._p("inbox")
    @property
    def work(self) -> Path: return self._p("work")
    @property
    def data_dir(self) -> Path: return self._p("data")
    @property
    def out_dir(self) -> Path: return self._p("out")
    @property
    def db_path(self) -> Path: return self.data_dir / "qb.sqlite"
    @property
    def backups_dir(self) -> Path: return self.data_dir / "backups"
    @property
    def log_path(self) -> Path: return self.data_dir / "qb.log"
    @property
    def cache_dir(self) -> Path: return self.work / "cache"
    @property
    def settings_path(self) -> Path: return self.root / "settings.json"
    @property
    def keys_path(self) -> Path: return self.root / self.get("gemini", "keys_file", "keys.txt")
    @property
    def env_path(self) -> Path: return self.root / ".env"
    @property
    def subjects(self) -> dict: return self.raw["subjects"]
    @property
    def extract_models(self) -> list: return list(self.get("gemini", "extract_models"))

    def resolve(self, stored) -> Path:
        """A path stored in the database -> where it is now. New entries are stored relative to the app folder; old absolute
        ones are re-anchored at their 'work' or 'inbox' folder, so moving the whole app folder (another PC, Google Drive) works."""
        p = Path(str(stored))
        if not p.is_absolute():
            return self.root / p
        if p.exists():
            return p
        parts = p.parts
        for anchor, base in ((self.get("paths", "work", "work"), self.work), (self.get("paths", "inbox", "inbox"), self.inbox)):
            if anchor in parts:
                i = len(parts) - 1 - parts[::-1].index(anchor)
                cand = base.joinpath(*parts[i + 1:])
                if cand.exists():
                    return cand
        return p

    def ensure_dirs(self) -> None:
        for p in (self.inbox, self.work, self.data_dir, self.out_dir, self.cache_dir, self.backups_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_config(root: str | Path | None = None) -> Config:
    root = Path(root) if root else Path.cwd()
    raw = copy.deepcopy(DEFAULTS)
    toml_file = root / "config.toml"
    if toml_file.exists():
        if tomllib is None:
            raise RuntimeError("Python 3.11+ is required (or `pip install tomli`).")
        with open(toml_file, "rb") as f:
            raw = _merge(raw, tomllib.load(f))
    sj = root / "settings.json"
    if sj.exists():
        try:
            raw = _merge(raw, json.loads(sj.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass            # a damaged settings.json must never stop the app from starting
    load_dotenv(root)
    return Config(root, raw)


def save_settings(root: str | Path, patch: dict) -> None:
    """Merge `patch` into settings.json (what the Settings page saves)."""
    path = Path(root) / "settings.json"
    current: dict = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            current = {}
    path.write_text(json.dumps(_merge(current, patch), ensure_ascii=False, indent=2), encoding="utf-8")


def reset_settings(root: str | Path) -> None:
    p = Path(root) / "settings.json"
    if p.exists():
        p.unlink()

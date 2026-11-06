"""Text helpers: Bengali digits, option labels, normalisation for duplicate detection."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
BN_LABELS = ["ক", "খ", "গ", "ঘ", "ঙ"]   # a few questions have a fifth option, e.g. "none of these"
_LABEL_MAP = {"ক": "ক", "খ": "খ", "গ": "গ", "ঘ": "ঘ", "ঙ": "ঙ", "a": "ক", "b": "খ", "c": "গ", "d": "ঘ", "e": "ঙ"}
_KEEP_SYMBOLS = set("+-−=*/^<>%")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def to_ascii_digits(s: str) -> str:
    return (s or "").translate(_BN_DIGITS)


def parse_number(s) -> int | None:
    """'৩৭.' / '37)' / ' ০১ ' -> int (None if no digits)."""
    m = re.search(r"\d+", to_ascii_digits(str(s or "")))
    return int(m.group()) if m else None


def canon_label(s) -> str | None:
    """Canonical option label (ক/খ/গ/ঘ) from things like '(খ)', 'ⓑ', 'b.'; None if unknown."""
    if s is None:
        return None
    t = unicodedata.normalize("NFKC", str(s)).strip().lower()
    t = re.sub(r"[\s().\[\]{}:;,\-–—]", "", t)
    if not t:
        return None
    return _LABEL_MAP.get(t[0])


def display_label(raw, fallback: str = "") -> str:
    """Option label as the source prints it: 'ক', 'A', 'b' -> 'B'. Falls back to the canonical Bengali label."""
    t = unicodedata.normalize("NFKC", str(raw or "")).strip()
    t = re.sub(r"[\s().\[\]{}:;,\-–—]", "", t)
    if not t:
        return fallback
    ch = t[0]
    return ch.upper() if ch.isascii() and ch.isalpha() else ch


def canon_key_answer(s) -> str | None:
    """Answer-key cell -> 'ক'/'খ'/'গ'/'ঘ', 'NOTE', or None (unreadable)."""
    if s is None:
        return None
    t = unicodedata.normalize("NFKC", str(s)).strip()
    if t.lower().startswith("note"):
        return "NOTE"
    return canon_label(t)


def norm_text(s: str) -> str:
    """Aggressive normalisation used for duplicate hashing and similarity.

    Keeps letters, digits, combining marks (needed for Bengali vowel signs) and
    math symbols; drops whitespace, punctuation and zero-width characters.
    """
    s = unicodedata.normalize("NFKC", s or "").translate(_ZERO_WIDTH)
    s = to_ascii_digits(s).lower()
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in "LNMS" or ch in _KEEP_SYMBOLS:
            out.append(ch)
    return "".join(out)


def similarity(a: str, b: str) -> float:
    na, nb = norm_text(a), norm_text(b)
    if not na and not nb:
        return 1.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def content_hash(stem: str, option_texts: list[str]) -> str:
    """Order-insensitive over options, so reshuffled options still count as the same question."""
    parts = [norm_text(stem)] + sorted(norm_text(o) for o in option_texts)
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()


def slug_tag(s: str, limit: int = 60) -> str:
    """Anki tags cannot contain spaces."""
    s = (s or "").replace("&", "and")
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r'["\']', "", s)
    return s[:limit] or "unknown"

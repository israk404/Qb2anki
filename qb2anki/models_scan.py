"""Find out which Gemini models actually work for your key(s), and suggest settings."""
from __future__ import annotations

import re

from .errors import GeminiError

_SKIP = ("tts", "live", "image", "audio", "transcribe", "embedding", "omni", "native", "robotics", "computer-use",
         "veo", "imagen", "translate", "learnlm", "aqa", "gemma", "deep-research")


def _rank(name: str):
    m = re.match(r"gemini-(\d+(?:\.\d+)?)-(.*)", name)
    ver = float(m.group(1)) if m else 0.0
    rest = m.group(2) if m else name
    return ("lite" in rest, "preview" in rest or "exp" in rest, -ver, name)


def candidates(names: list[str]) -> list[str]:
    return sorted((n for n in names if n.startswith("gemini-") and any(k in n for k in ("flash", "pro"))
                   and not any(x in n for x in _SKIP)), key=_rank)


def scan(client, say=print, should_stop=lambda: False) -> dict:
    """Tries every likely model with one tiny request. -> {'working': [...], 'suggest': {...} | None}"""
    cands = candidates(client.list_models())
    say(f"Testing {len(cands)} candidate models with one tiny request each (uses a little quota)...")
    schema = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}, "required": ["ok"]}
    working = []
    for m in cands:
        if should_stop():
            break
        try:
            r = client.generate_json(model=m, prompt='Reply with {"ok": true}', schema=schema, system=None, label=f"test {m}",
                                     allow_fallback=False)
            say(f"  OK       {m}" if r.get("ok") else f"  ODD      {m}: {r}")
            if r.get("ok"):
                working.append(m)
        except GeminiError as e:
            say(f"  FAILED   {m}: {str(e).split('] ', 1)[-1][:110]}")
    if not working:
        say("No model worked. Check your key and your connection.")
        return {"working": [], "suggest": None}
    two = working[:2] if len(working) > 1 else working * 2
    suggest = {"layout_model": working[0], "extract_models": two, "fill_model": working[0],
               "fallback_models": [m for m in working[2:5]]}
    say("")
    say(f"Suggested: layout/fill = {working[0]}; readers = {two[0]} and {two[1]}; fallbacks = {', '.join(suggest['fallback_models']) or '-'}")
    if len(working) < 2:
        say("Only one model works, so the optional cross-check would not be independent.")
    return {"working": working, "suggest": suggest}

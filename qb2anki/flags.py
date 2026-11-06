"""Flags: every doubt about a card becomes one.

hard flag -> the card goes to the review queue and is NOT exported until you approve it
soft flag -> the card is exportable, but the note is visible in the review tool
"""
from __future__ import annotations


def mkflag(code: str, msg: str, hard: bool = True) -> dict:
    return {"code": code, "msg": msg, "hard": hard}


def finalize_status(q: dict, block_soft: bool = False) -> str:
    hard = any(f["hard"] for f in q["flags"])
    soft = any(not f["hard"] for f in q["flags"])
    return "review" if hard or (block_soft and soft) else "ok"

"""Command line (the app does the same with buttons; run `python -m qb2anki` or start.bat to open the app).

  ui        open the app (default)
  run       read the photos waiting in inbox\\
  export    write the Anki import file
  status    counts, failed photos and today's usage
  rebuild   re-apply the current rules to every photo already read (uses saved answers)
  fill      let the AI fill missing answers/explanations of stored cards
  proofread let the AI proofread stored cards against their page pictures
  check     full AI cross-check of stored cards (--all: also cards checked before)
  keys      status of keys.txt;  --check tests every key;  --reset clears the marks
  doctor    test the configured models
  models    list the Gemini models your key can use;  --test finds the ones that work
  bundle    zip diagnostics (never contains keys)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import load_config
from .db import DB, quota_day, quota_reset_hint
from .errors import GeminiError


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _setup_logging(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(cfg.log_path, encoding="utf-8")])
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    logging.getLogger().addHandler(console)


def print_usage(db) -> None:
    rows = db.usage_for(quota_day())
    if rows:
        print("\nRequests made by this tool today (free quotas reset at midnight Pacific time):")
        for r in rows:
            print(f"  {r['model']}: {r['calls']} requests, {r['input_tokens']:,} input / {r['output_tokens']:,} output tokens")


def _client(cfg, db, use_cache=True):
    from .gemini_client import GeminiClient
    return GeminiClient(cfg, use_cache=use_cache, db=db)


def cmd_ui(cfg, args):
    from .ui import serve
    serve(cfg.root, open_browser=not args.no_browser)


def cmd_run(cfg, args):
    from .pipeline import Pipeline
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    pipe = Pipeline(cfg, db, _client(cfg, db, not args.no_cache))
    files = [Path(f) for f in args.files] if args.files else None
    if files is None and not pipe.list_inbox():
        print(f"No photos found in {cfg.inbox}. Add photos there (or use the app) and run again.")
        return
    rotate = args.rotate if args.rotate in (None, "auto") else int(args.rotate)
    from .maintenance import auto_backup
    b = auto_backup(cfg, db)
    if b:
        print(f"Automatic backup saved: {b.name}")
    rep = pipe.run(files, rotate=rotate, limit=args.limit, profile=args.type)
    print(f"\nphotos read: {rep['photos']}   already done: {rep['skipped']}")
    print(f"questions: {rep['inserted']} new, {rep['duplicate']} duplicates merged, {rep['review']} need review")
    for name, msg in rep["failed"]:
        print(f"FAILED {name}: {msg}")
    if rep.get("quota_stop"):
        print("\nSTOPPED EARLY: " + rep["quota_stop"])
        if "blocked" not in rep["quota_stop"].lower():
            print(f"Free quotas reset at midnight Pacific time, which is {quota_reset_hint()} for you. Finished work is saved.")
    print_usage(db)
    db.close()


def cmd_export(cfg, args):
    from .export_anki import export
    db = DB(cfg.db_path)
    r = export(cfg, db, include_all=args.all, anki_media=args.anki_media)
    if r["path"]:
        print(f"Wrote {r['count']} cards -> {r['path']}")
        print("In Anki: File > Import, choose that file (once). Images: copy out\\media\\* into Anki's collection.media folder.")
    else:
        print("Nothing to export (no approved/ok cards that were not already exported).")
    for qid, why in r["skipped"]:
        print(f"skipped card #{qid}: {why}")
    db.close()


def cmd_status(cfg, args):
    db = DB(cfg.db_path)
    s = db.stats()
    print(f"photos: {s['photos']} (failed: {s['failed_photos']})")
    print("cards:", {k: v for k, v in s.items() if k in ('ok', 'review', 'approved', 'rejected')}, f"exported: {s['exported']}")
    for r in db.conn.execute("SELECT path, error FROM photos WHERE status='failed'"):
        print(f"FAILED {Path(r['path']).name}: {r['error']}")
    print_usage(db)
    db.close()


def cmd_rebuild(cfg, args):
    from .pipeline import Pipeline, PipelineError
    db = DB(cfg.db_path)
    client = _client(cfg, db)
    pipe = Pipeline(cfg, db, client, say=lambda *_: None)
    done = skipped = 0
    for row in db.conn.execute("SELECT * FROM photos WHERE status='done'").fetchall():
        try:
            rep = pipe.rerun(row["id"], force=args.force)
            done += 1
            print(f"{row['stem']}: {rep['inserted']} questions, {rep['review']} need review")
        except PipelineError as e:
            skipped += 1
            print(f"{row['stem']}: skipped ({e})")
    print(f"\nRebuilt {done} photo(s), skipped {skipped}. Requests used: {client.api_calls} (0 = all from saved answers).")
    db.close()


def cmd_fill(cfg, args):
    from .fill import fill_database
    db = DB(cfg.db_path)
    client = _client(cfg, db)
    r = fill_database(db, client, cfg.get("gemini", "fill_model") or cfg.extract_models[0], say=print)
    print(f"AI wrote {r['answered']} answer(s) and {r['explained']} explanation(s) for {r['candidates']} card(s) with gaps.")
    db.close()


def cmd_proofread(cfg, args):
    from .maintenance import auto_backup
    from .proofread import proofread_database
    db = DB(cfg.db_path)
    auto_backup(cfg, db)
    r = proofread_database(cfg, db, _client(cfg, db), cfg.get("gemini", "fill_model") or cfg.extract_models[0], say=print)
    print(f"{r['pages']} page(s) checked, {r['changed']} card(s) corrected.")
    db.close()


def cmd_check(cfg, args):
    from .factcheck import check_database
    from .maintenance import auto_backup
    db = DB(cfg.db_path)
    auto_backup(cfg, db)
    r = check_database(cfg, db, _client(cfg, db), cfg.get("gemini", "fill_model") or cfg.extract_models[0], say=print,
                       recheck=args.all)
    print(f"{r['checked']} card(s) checked; {r['held']} wait in Review because the AI disagreed with the source.")
    db.close()


def cmd_keys(cfg, args):
    from collections import Counter
    from .keys import KeyPool
    db = DB(cfg.db_path)
    if args.reset:
        db.clear_key_states()
        print("Cleared the 'invalid / blocked' marks and the halt.")
    pool = KeyPool.from_config(cfg, db, any_mode=True)
    if pool is None:
        print("No key list: put two or more keys in keys.txt next to config.toml.")
        return
    if args.check:
        client = _client(cfg, db, False)
        model = cfg.get("gemini", "layout_model")
        counts: Counter = Counter()
        for k in pool.keys:
            status, note = client.check_key(k.secret, model)
            counts[status] += 1
            if status in ("bad", "blocked"):
                pool.on_key_rejected(k, status, note, halt=False)
            print(f"  {k.label} ...{k.secret[-4:]}: {status}" + (f"  ({note})" if note and status != "ok" else ""))
            time.sleep(0.6)
        pool.flush(db)
        print("Result: " + ", ".join(f"{n} {st}" for st, n in counts.most_common()))
    s = pool.summary()
    print("Rotation is " + ("ON" if str(cfg.get("gemini", "key_mode", "single")) == "rotate" else "OFF (Settings: Use one key)") + ".")
    print(f"Keys: {s['keys']} loaded, {s['bad']} invalid, {s['blocked']} blocked. Today: {s['calls_today']} requests over {s['keys_used_today']} keys.")
    db.close()


def cmd_doctor(cfg, args):
    import importlib
    ok = True
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    if not ((3, 11) <= sys.version_info[:2] <= (3, 13)):
        ok = False
        print("   PROBLEM: use Python 3.11, 3.12 or 3.13 (newer versions lack builds of some packages).")
    for mod, pip_name in (("google.genai", "google-genai"), ("PIL.Image", "pillow"), ("pydantic", "pydantic")):
        try:
            importlib.import_module(mod)
            print(f"package {pip_name}: OK")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"package {pip_name}: BROKEN or MISSING -> {e}\n   fix: {Path(sys.executable).name} -m pip install --force-reinstall --no-cache-dir {pip_name}")
    if not ok:
        return
    cfg.ensure_dirs()
    client = _client(cfg, None, False)
    schema = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}, "required": ["ok"]}
    for m in dict.fromkeys([cfg.get("gemini", "layout_model"), *cfg.extract_models, cfg.get("gemini", "fill_model")]):
        try:
            r = client.generate_json(model=m, prompt='Reply with {"ok": true}', schema=schema, system=None, label="doctor", allow_fallback=False)
            print(f"model {m}: OK" if r.get("ok") else f"model {m}: unexpected reply {r}")
        except GeminiError as e:
            print(f"model {m}: FAILED - {str(e).split('] ', 1)[-1]}")


def cmd_models(cfg, args):
    from . import models_scan
    client = _client(cfg, None, False)
    if args.test:
        models_scan.scan(client)
    else:
        print("\n".join(client.list_models()))
        print("\nTip: `models --test` shows which of these really work for your key.")


def cmd_bundle(cfg, args):
    from .maintenance import make_bundle
    out = make_bundle(cfg, with_spreads=args.with_spreads, no_images=args.no_images)
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB). It never contains your API keys.")


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="qb2anki", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="folder that holds config.toml (default: current folder)")
    sub = ap.add_subparsers(dest="cmd")
    u = sub.add_parser("ui")
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(fn=cmd_ui)
    r = sub.add_parser("run")
    r.add_argument("files", nargs="*")
    r.add_argument("--rotate", help="0/90/180/270 or 'auto' (default: from the profile)")
    r.add_argument("--limit", type=int)
    r.add_argument("--type", help="read every photo as this type: auto (detect), book or screenshot")
    r.add_argument("--no-cache", action="store_true")
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("export")
    e.add_argument("--all", action="store_true")
    e.add_argument("--anki-media")
    e.set_defaults(fn=cmd_export)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    rb = sub.add_parser("rebuild")
    rb.add_argument("--force", action="store_true")
    rb.set_defaults(fn=cmd_rebuild)
    sub.add_parser("fill").set_defaults(fn=cmd_fill)
    sub.add_parser("proofread").set_defaults(fn=cmd_proofread)
    ck = sub.add_parser("check")
    ck.add_argument("--all", action="store_true", help="also re-check cards checked before")
    ck.set_defaults(fn=cmd_check)
    k = sub.add_parser("keys")
    k.add_argument("--check", action="store_true")
    k.add_argument("--reset", action="store_true")
    k.set_defaults(fn=cmd_keys)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    m = sub.add_parser("models")
    m.add_argument("--test", action="store_true")
    m.set_defaults(fn=cmd_models)
    b = sub.add_parser("bundle")
    b.add_argument("--with-spreads", action="store_true")
    b.add_argument("--no-images", action="store_true")
    b.set_defaults(fn=cmd_bundle)

    args = ap.parse_args(argv)
    cfg = load_config(args.root)
    _setup_logging(cfg)
    if args.cmd is None:
        args = ap.parse_args((["--root", args.root] if args.root else []) + ["ui"])
    try:
        args.fn(cfg, args)
    except GeminiError as e:
        print(f"\nERROR: {e}")
        return 1
    except ImportError as e:
        print(f"\nERROR: a required package is broken or missing: {e}")
        if sys.version_info[:2] > (3, 13):
            print(f"Your Python is {sys.version.split()[0]}, which is too new. Install Python 3.12, delete the .venv folder and run setup.bat again.")
        print("Run setup.bat again, or: python -m qb2anki doctor")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

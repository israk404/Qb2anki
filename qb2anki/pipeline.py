"""The reading pipeline.

For every photo:
  1. rotate it, and (for book spreads) find the printed pages; screenshots are one page already
  2. read each page in ONE request: questions, answer key and exam title together (optionally twice, see cross_check)
  3. assemble + validate (flags), check/repair the math, let the AI fill missing answers/explanations
  4. store in the database (duplicates are merged)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from . import assemble, fill, imaging, mathcheck, proofread, prompts
from .db import quota_day
from .errors import NoQuotaForModel, QuotaExhausted
from .flags import finalize_status, mkflag
from .gemini_client import GeminiClient, GeminiError
from .imaging import LayoutError
from .schemas import (DETECT_SCHEMA, LAYOUT_SCHEMA, ORIENTATION_SCHEMA, Detection, LayoutModel, Orientation, PageModel,
                      page_schema)

log = logging.getLogger("qb2anki.pipeline")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_REREADS = 12          # vision re-reads of broken math per photo


class PipelineError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Pipeline:
    def __init__(self, cfg, db, client: GeminiClient, say=print, should_stop=lambda: False):
        self.cfg, self.db, self.client, self.say, self.should_stop = cfg, db, client, say, should_stop
        self.models = cfg.extract_models
        self.cross_check = bool(cfg.get("checks", "cross_check", False)) and len(self.models) > 1
        self.math_check = bool(cfg.get("checks", "math_check", True))
        self.proofread = bool(cfg.get("checks", "proofread", True))
        self.profile_override: str | None = None       # e.g. "auto": detect the type of every photo in this run
        self.ai_fill = bool(cfg.get("checks", "ai_fill", True))
        self.profiles = cfg.raw.get("profiles", {})

    # ------------------------------------------------------------------ helpers
    def _rel(self, p: Path) -> str:
        return os.path.relpath(p, self.cfg.root).replace("\\", "/")

    def _workdir(self, path: Path, sha: str) -> Path:
        row = self.db.photo_by_stem(path.stem)
        name = path.stem if (row is None or row["sha256"] == sha) else f"{path.stem}_{sha[:8]}"
        d = self.cfg.work / name
        (d / "crops").mkdir(parents=True, exist_ok=True)
        return d

    def _par(self, tasks: dict) -> dict:
        if not tasks:
            return {}
        workers = max(1, int(self.cfg.get("gemini", "concurrency", 2)))
        results, errors, quota = {}, [], None
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {name: ex.submit(fn) for name, fn in tasks.items()}
            for name, fut in futs.items():
                try:
                    results[name] = fut.result()
                except (QuotaExhausted, NoQuotaForModel) as e:
                    quota = quota or e
                    errors.append(f"{name}: {e}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}: {e}")
        if quota is not None:      # stop the whole run: more requests today would only fail
            raise quota
        if errors:
            raise PipelineError("; ".join(errors))
        return results

    # ------------------------------------------------------------------ source profiles
    def resolve_profile(self, path: Path):
        """-> (profile name, profile settings, exam hint).

        inbox\\file.jpg                      default profile, exam from the page title
        inbox\\screenshot\\file.png          that profile
        inbox\\screenshot\\BCS 45\\file.png   that profile; 'BCS 45' names the exam when the page has no title
        inbox\\BCS 45\\file.png              default profile; 'BCS 45' names the exam
        """
        names = {k.lower(): k for k, v in self.profiles.items() if isinstance(v, dict)}
        name, hint = self.profiles.get("default", "book"), None
        try:
            parts = list(Path(path).resolve().relative_to(self.cfg.inbox.resolve()).parts[:-1])
        except ValueError:
            parts = []
        if parts and parts[0].lower() in names:
            name = names[parts[0].lower()]
            hint = parts[-1] if len(parts) > 1 else None
        elif parts:
            hint = parts[-1]
        if self.profile_override and isinstance(self.profiles.get(self.profile_override), dict):
            name = self.profile_override
        return name, dict(self.profiles.get(name, {})), hint

    # ------------------------------------------------------------------ Phase A: rotate, find pages
    def detect_rotation(self, img0: Image.Image) -> int:
        small = imaging.shrink(img0, 1400)
        res = self.client.generate_json(model=self.cfg.get("gemini", "layout_model"), prompt=prompts.ORIENTATION,
                                        schema=ORIENTATION_SCHEMA, image=small, label="orientation")
        return int(Orientation.model_validate(res).rotate_ccw_degrees) % 360

    def detect(self, img0: Image.Image) -> Detection:
        """What kind of image is this (book spread / single page / screenshot) and which way must it turn? 1 small request."""
        small = imaging.shrink(img0, 1400)
        res = self.client.generate_json(model=self.cfg.get("gemini", "layout_model"), prompt=prompts.DETECT,
                                        schema=DETECT_SCHEMA, image=small, label="detect")
        return Detection.model_validate(res)

    def find_layout(self, spread: Image.Image) -> list[dict]:
        small = imaging.shrink(spread, int(self.cfg.get("image", "layout_max_width", 2000)))
        W, H = spread.size
        tried = []
        for model in dict.fromkeys([self.cfg.get("gemini", "layout_model"), self.models[0]]):
            res = self.client.generate_json(model=model, prompt=prompts.LAYOUT, schema=LAYOUT_SCHEMA, image=small, label="layout")
            pages, problems = imaging.validate_layout(LayoutModel.model_validate(res), W, H)
            if not problems:
                return pages
            tried.append(f"{model}: {'; '.join(problems)}")
            self.say(f"   layout from {model} rejected: {'; '.join(problems)}")
        raise LayoutError(" | ".join(tried))

    def prepare_photo(self, path: Path, rotate=None) -> int | None:
        sha = sha256_file(path)
        row = self.db.photo_by_sha(sha)
        if row and row["status"] == "done":
            self.say(f"- {path.name}: already done, skipping")
            return None
        photo_id = row["id"] if row else self.db.add_photo(sha, self._rel(path), path.stem, 0)
        wd = self._workdir(path, sha)
        pname, prof, hint = self.resolve_profile(path)
        img0 = imaging.load_image(path)
        if prof.get("detect") and rotate is None:
            det = self.detect(img0)
            rot, use_layout, kind = det.rotate_ccw_degrees, det.kind == "spread", det.kind
            reader = "book" if use_layout else "general"
            self.say(f"- {path.name}: detected {kind}" + (f", turned {rot}°" if rot else ""))
        else:
            use_layout = bool(prof.get("layout", True))
            reader = prof.get("reader", "book" if use_layout else "general")
            kind = "spread" if use_layout else "page"
            rot = prof.get("rotate", 90) if rotate is None else rotate
            if str(rot).lower() == "auto":
                rot = self.detect_rotation(img0)
                self.say(f"   auto rotation: {rot} degrees counter-clockwise")
            self.say(f"- {path.name}: {'finding pages' if use_layout else 'one page per image (' + pname + ')'}")
        rot = int(rot)
        spread = imaging.rotate_ccw(img0, rot)
        if use_layout:
            try:
                pages = self.find_layout(spread)
            except LayoutError as e:
                if rotate is not None or "not upright" not in str(e):
                    raise
                rot2 = self.detect_rotation(img0)          # the photo type's usual turn did not fit this photo: find the right one
                if rot2 == rot:
                    raise
                self.say(f"   the photo needed a different turn ({rot2}°); trying again")
                rot, spread = rot2, imaging.rotate_ccw(img0, rot2)
                pages = self.find_layout(spread)
            spread.save(wd / "spread.jpg", quality=95, subsampling=0)
            imaging.draw_overlay(spread, pages).save(wd / "layout_overlay.jpg", quality=88)
        else:       # screenshot / single page: the whole image is ONE page, so no layout request is needed
            spread.save(wd / "spread.jpg", quality=95, subsampling=0)
            pages = [dict(imaging.WHOLE_PAGE)]
            imaging.shrink(spread, 1800).save(wd / "layout_overlay.jpg", quality=88)
        self.db.update_photo(photo_id, path=self._rel(path), rotate=rot, layout_json=json.dumps(pages), status="layout_done",
                             error=None, workdir=self._rel(wd), profile=pname, exam_hint=hint, reader=reader, kind=kind)
        return photo_id

    # ------------------------------------------------------------------ Phase B: read, check, fill
    def _read_page(self, model: str, img: Image.Image, label: str, reader: str) -> PageModel:
        prompt = prompts.general_page_prompt(self.cfg.subjects) if reader == "general" else prompts.page_prompt(self.cfg.subjects)
        res = self.client.generate_json(model=model, prompt=prompt, schema=page_schema(list(self.cfg.subjects)), image=img, label=label)
        return PageModel.model_validate(res)

    def _run_jobs(self, jobs, runner, tag: str) -> list:
        """Run (id_map, payload) jobs in parallel; -> results in job order."""
        res = self._par({f"{tag}{i}": (lambda p=payload, l=f"{tag} {i}": runner(p, l)) for i, (_, payload) in enumerate(jobs)})
        return [res[f"{tag}{i}"] for i in range(len(jobs))]

    def _proofread(self, assembled, page_imgs: dict) -> None:
        """One request per page: the reading is compared with the page picture and reading errors are corrected."""
        model = self.cfg.get("gemini", "fill_model") or self.models[0]
        jobs = {}
        for pi, (qs, _) in enumerate(assembled):
            id_map, payload = proofread.payload_for(qs)
            if payload:
                jobs[pi] = (id_map, payload)
        if not jobs:
            return
        self.say(f"   proofreading {len(jobs)} page(s) against the photo")
        res = self._par({f"proof{pi}": (lambda pi=pi, p=payload: proofread.run(self.client, model, page_imgs[pi], p, f"proof p{pi}"))
                         for pi, (_, payload) in jobs.items()})
        n = sum(proofread.apply(jobs[pi][0], res[f"proof{pi}"]) for pi in jobs)
        if n:
            self.say(f"   proofreading corrected {n} question(s)")

    def _check_math(self, qs: list[dict], page_imgs: dict) -> None:
        model = self.cfg.get("gemini", "fill_model") or self.models[0]
        jobs = mathcheck.build_check_jobs(qs)
        if not jobs:
            return
        self.say(f"   math check: {sum(len(m) for m, _ in jobs)} explanation(s)")
        for (id_map, _), mm in zip(jobs, self._run_jobs(jobs, lambda p, l: mathcheck.run_check(self.client, model, p, l), "math")):
            mathcheck.apply_check(id_map, mm)
        # whatever is still broken: read its picture again, focused on the solution
        broken = [(q, mathcheck.still_broken(q)) for q in qs if mathcheck.has_math(q)]
        broken = [(q, p) for q, p in broken if p and imaging.valid_box(q.get("question_box")) and q.get("_pi") in page_imgs][:MAX_REREADS]
        if not broken:
            return
        self.say(f"   math re-read from the picture: {len(broken)} question(s)")
        tasks = {f"reread{i}": (lambda q=q, p=p, l=f"reread {i}": mathcheck.run_reread(
            self.client, model, imaging.crop_norm(page_imgs[q["_pi"]], q["question_box"], 0.004), p, l))
            for i, (q, p) in enumerate(broken)}
        res = self._par(tasks)
        changed = []
        for i, (q, _) in enumerate(broken):
            if mathcheck.apply_reread(q, res[f"reread{i}"]):
                q["flags"] = [f for f in q["flags"] if f["code"] != "math_inconsistent"]      # re-judged below
                changed.append(q)
        jobs = mathcheck.build_check_jobs(changed)
        for (id_map, _), mm in zip(jobs, self._run_jobs(jobs, lambda p, l: mathcheck.run_check(self.client, model, p, l), "math2")):
            mathcheck.apply_check(id_map, mm)

    def _fill(self, qs: list[dict]) -> None:
        jobs = fill.build_jobs(qs)
        if not jobs:
            return
        model = self.cfg.get("gemini", "fill_model") or self.models[0]
        self.say(f"   AI fill: {sum(len(m) for m, _ in jobs)} question(s)")
        for (id_map, _), fm in zip(jobs, self._run_jobs(jobs, lambda p, l: fill.run_job(self.client, model, p, l), "fill")):
            fill.apply_job(id_map, fm)
        for q in qs:                       # an AI-written explanation may itself contain broken math
            if q.get("ai_explanation") and mathcheck.hard_problems(q["ai_explanation"]):
                q["flags"].append(mkflag("ai_unsure", "the AI-written explanation has formulas that may not render: check it", hard=False))

    def extract_photo(self, photo_id: int) -> dict:
        photo = self.db.photo(photo_id)
        wd = self.cfg.resolve(photo["workdir"])
        spread = Image.open(wd / "spread.jpg").convert("RGB")
        W, H = spread.size
        pages = json.loads(photo["layout_json"])
        pad = float(self.cfg.get("image", "crop_pad", 0.006))
        up = float(self.cfg.get("image", "upscale", 1.0))
        reader = photo["reader"] or self.profiles.get(photo["profile"] or "book", {}).get("reader", "book")

        tasks, page_imgs = {}, {}
        for pi, page in enumerate(pages):
            img = imaging.crop_px(spread, imaging.to_px(page["page_box"], W, H), pad=int(pad * W), upscale=up)
            img.save(wd / "crops" / f"p{pi}_page.jpg", quality=92)
            page_imgs[pi] = img
            tasks[f"p{pi}A"] = lambda m=self.models[0], i=img, l=f"page p{pi}": self._read_page(m, i, l, reader)
            if self.cross_check:
                tasks[f"p{pi}B"] = lambda m=self.models[1], i=img, l=f"page B p{pi}": self._read_page(m, i, l, reader)
        if hasattr(self.client, "warm_up"):
            self.client.warm_up()
        self.say(f"   reading {len(pages)} page(s){' twice' if self.cross_check else ''}")
        res = self._par(tasks)

        checks = self.cfg.raw["checks"]
        assembled, headers = [], []
        for pi in range(len(pages)):
            pa, pb = res[f"p{pi}A"], res.get(f"p{pi}B")
            qs, pflags = assemble.assemble_page(pa, pb, self.cfg.subjects, checks)
            for q in qs:
                q["_pi"] = pi
            assembled.append((qs, pflags))
            headers.append(pa.exam_header if pa.exam_header and (pa.exam_header.title or "").strip() else None)

        all_qs = [q for qs, _ in assembled for q in qs]
        if self.proofread:
            self._proofread(assembled, page_imgs)
        if self.math_check:
            self._check_math(all_qs, page_imgs)
        if self.ai_fill:
            self._fill(all_qs)
        return self._store(photo_id, wd, pages, assembled, headers, page_imgs, {pi: res[f"p{pi}A"] for pi in range(len(pages))})

    def _store(self, photo_id, wd: Path, pages, assembled, headers, page_imgs, readings) -> dict:
        photo = self.db.photo(photo_id)
        hint = (photo["exam_hint"] or "").strip()
        block_soft = bool(self.cfg.get("checks", "block_export_on_soft_flags", False))
        near_ratio = float(self.cfg.get("checks", "near_duplicate_ratio", 0.92))
        counts = {"inserted": 0, "duplicate": 0, "review": 0}
        prev_exam = None
        for pi, page in enumerate(pages):
            qs, pflags = assembled[pi]
            printed = page.get("printed_page")
            hdr = headers[pi]
            if hdr is not None:
                info = {"duration": hdr.duration, "total_marks": hdr.total_marks, "other": hdr.other_text}
                exam_id = self.db.get_or_create_exam(hdr.title, hdr.exam_date, info)
            else:
                exam_id = self.db.exam_of_printed_page(printed - 1) if printed else None
                if exam_id is None and pi > 0 and (printed is None or pages[pi - 1].get("printed_page") == printed - 1):
                    exam_id = prev_exam
            if exam_id is None and hint:
                exam_id = self.db.get_or_create_exam(hint, None, {"source": "folder name"})
            prev_exam = exam_id
            key_json = {"entries": [e.model_dump() for e in readings[pi].answer_key]}
            page_id = self.db.add_page(photo_id, pi, printed, exam_id, hdr is not None, key_json, pflags)

            for q in qs:
                for f in pflags:
                    concerns = f.get("near")           # None/empty = concerns the whole page
                    if not concerns or q["number"] in concerns:
                        q["flags"].append({k: v for k, v in f.items() if k != "near"})
                if exam_id is None:
                    q["flags"].append(mkflag("exam_unknown", "exam not identified yet (title not seen)", hard=False))
                img = page_imgs[pi]
                tag = f"p{pi}_q{q['number'] if q['number'] is not None else 'x'}"
                qc = imaging.crop_norm(img, q["question_box"], 0.004) if imaging.valid_box(q["question_box"]) else img
                qc.save(wd / "crops" / f"{tag}.jpg", quality=90)
                q["crop_path"] = self._rel(wd / "crops" / f"{tag}.jpg")
                if q["has_figure"]:
                    if imaging.valid_box(q["figure_box"]):
                        fp = wd / "crops" / f"fig_{tag}.jpg"
                        imaging.crop_norm(img, q["figure_box"], 0.004).save(fp, quality=92)
                        q["figure_path"] = self._rel(fp)
                    else:
                        q["flags"].append(mkflag("figure_box_missing", "figure present but its box was not usable"))
                q.pop("_proof_changed", None)
                status = finalize_status(q, block_soft)
                _, outcome = self.db.ingest_question(q, page_id=page_id, photo_id=photo_id, exam_id=exam_id,
                                                     printed_page=printed, status=status, near_dup_ratio=near_ratio)
                counts[outcome] += 1
                if outcome == "inserted" and status == "review":
                    counts["review"] += 1
        self.db.update_photo(photo_id, status="done", error=None)
        self.db.resolve_exams()
        return counts

    # ------------------------------------------------------------------ run
    def list_inbox(self) -> list[Path]:
        if not self.cfg.inbox.exists():
            return []
        return sorted(p for p in self.cfg.inbox.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT)

    def pending(self) -> list[Path]:
        """Inbox files that have not been read successfully yet."""
        out = []
        for f in self.list_inbox():
            row = self.db.photo_by_sha(sha256_file(f))
            if not row or row["status"] != "done":
                out.append(f)
        return out

    def flush_usage(self) -> None:
        """Save this run's request/token counts and the key pool's state."""
        pool = getattr(self.client, "pool", None)
        if pool is not None:
            pool.flush(self.db)
        log_ = getattr(self.client, "usage_log", None)
        if not log_:
            return
        agg: dict[str, list[int]] = {}
        for model, t_in, t_out in list(log_):
            a = agg.setdefault(model, [0, 0, 0])
            a[0] += 1
            a[1] += t_in
            a[2] += t_out
        log_.clear()
        day = quota_day()
        for model, (calls, t_in, t_out) in agg.items():
            self.db.add_usage(day, model, calls, t_in, t_out)

    def run(self, files: list[Path] | None = None, rotate=None, limit: int | None = None, profile: str | None = None) -> dict:
        self.profile_override = profile or None
        try:
            return self._run(files, rotate, limit)
        finally:
            self.profile_override = None
            self.flush_usage()

    def _run(self, files, rotate, limit) -> dict:
        files = files if files is not None else self.list_inbox()
        report = {"photos": 0, "failed": [], "inserted": 0, "duplicate": 0, "review": 0, "skipped": 0, "quota_stop": None,
                  "stopped": False}
        prepared = []
        for f in files:
            if limit is not None and len(prepared) >= limit:
                break
            if self.should_stop():
                report["stopped"] = True
                break
            try:
                pid = self.prepare_photo(f, rotate)
            except (QuotaExhausted, NoQuotaForModel) as e:
                self._fail(f, e, report)
                report["quota_stop"] = str(e)
                break
            except (LayoutError, GeminiError, PipelineError) as e:
                self._fail(f, e, report)
                continue
            except Exception as e:  # noqa: BLE001  (an odd model answer or a broken file must not stop the other photos)
                log.exception("unexpected error preparing %s", f)
                self._fail(f, RuntimeError(f"unexpected problem: {type(e).__name__}: {e}"), report)
                continue
            if pid is None:
                report["skipped"] += 1
            else:
                prepared.append((f, pid))

        def order(item):
            pages = json.loads(self.db.photo(item[1])["layout_json"] or "[]")
            nums = [p["printed_page"] for p in pages if p.get("printed_page") is not None]
            return (min(nums) if nums else 10 ** 9, item[0].name)

        for f, pid in sorted(prepared, key=order):
            if report["quota_stop"]:
                break
            if self.should_stop():
                report["stopped"] = True
                break
            self.say(f"- {f.name}: reading")
            try:
                c = self.extract_photo(pid)
            except (QuotaExhausted, NoQuotaForModel) as e:
                self._fail(f, e, report, pid)
                report["quota_stop"] = str(e)
                break
            except (GeminiError, PipelineError, LayoutError) as e:
                self._fail(f, e, report, pid)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("unexpected error reading %s", f)
                self._fail(f, RuntimeError(f"unexpected problem: {type(e).__name__}: {e}"), report, pid)
                continue
            report["photos"] += 1
            for k in ("inserted", "duplicate", "review"):
                report[k] += c[k]
            self.say(f"   done: {c['inserted']} new, {c['duplicate']} duplicates merged, {c['review']} need review")
        return report

    def _fail(self, f: Path, e: Exception, report: dict, pid: int | None = None):
        msg = str(e)
        self.say(f"   FAILED {f.name}: {msg}")
        report["failed"].append((f.name, msg))
        row = self.db.photo(pid) if pid else self.db.photo_by_sha(sha256_file(f))
        if row:
            self.db.update_photo(row["id"], status="failed", error=msg[:2000])

    def rerun(self, photo_id: int, force: bool = False, rotate=None) -> dict:
        row = self.db.photo(photo_id)
        if not row:
            raise PipelineError(f"no photo #{photo_id} in the database")
        if self.db.photo_has_protected_questions(row["id"]) and not force:
            raise PipelineError("this photo has approved or exported questions; use force to redo it anyway")
        path = self.cfg.resolve(row["path"])
        if not path.exists():
            raise PipelineError(f"the photo file is gone: {path}")
        self.db.delete_photo_data(row["id"])
        self.db.update_photo(row["id"], status="new")
        return self.run([path], rotate=rotate)

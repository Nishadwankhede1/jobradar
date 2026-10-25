"""RankOne X apply packs: for every relevant job, the best truthful resume (original design), cover letter and form answers.

Runs only when the private RankOne X code and your profile are available (cloud: RANKONEX_DIR + RANKONE_PROFILE_FILE,
set up by the scan workflow); otherwise JobRadar's own tailored CV is used as before.

    alert first (speed) → pack right after → "Resume ready · rank #2" follow-up with the PDF
Jobs whose posting text is too short (e-mail leads) get a pack built from earlier postings with the same title
("proxy"); it is rebuilt when the real description arrives. The pack's resume replaces the tailored CV, so the
dashboard link, the encrypted site and the Telegram attachment all carry the RankOne X version.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..util import log, now_iso
from .service import cv_filename

SHORT = 400  # characters: below this the posting text is a title/teaser, not a description


def rankonex() -> tuple[Path, Path] | None:
    code, prof = os.environ.get("RANKONEX_DIR"), os.environ.get("RANKONE_PROFILE_FILE")
    if code and prof and Path(code, "rankone").is_dir() and Path(prof).is_file():
        return Path(code), Path(prof)
    return None


def _run(code: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(code), "PYTHONUTF8": "1",
           "RANKONE_DATA_DIR": tempfile.mkdtemp(prefix="rankone_")}  # throw-away database per pack
    return subprocess.run([sys.executable, "-m", "rankone", *args], cwd=code, env=env, capture_output=True, text=True, timeout=timeout)


def _job_text(db, job: dict, code: Path) -> tuple[str, bool]:
    desc = (job.get("description") or "").strip()
    if len(desc) >= SHORT:
        return f"Job title: {job.get('title')}\nCompany: {job.get('company_display')}\nLocation: {job.get('location') or ''}\n\n{desc}", False
    # too short: stand-in from earlier postings with the same title (same company first)
    rows = db.q("""SELECT description FROM jobs WHERE norm_title=? AND id!=? AND LENGTH(COALESCE(description,'')) >= ?
                   ORDER BY (company_id=?) DESC, first_seen_at DESC LIMIT 8""",
                (job.get("norm_title"), job["id"], SHORT, job.get("company_id")))
    if len(rows) < 2:
        return "", True
    tmp = Path(tempfile.mkdtemp(prefix="proxy_"))
    files = []
    for i, r in enumerate(rows):
        f = tmp / f"{i}.txt"
        f.write_text(r["description"], encoding="utf-8")
        files.append(str(f))
    p = _run(code, ["proxy", "--title", job.get("title") or "", "--company", job.get("company_display") or "", "--examples", *files], 120)
    return (p.stdout if p.returncode == 0 else ""), True


def build_packs(db, settings, root: Path, budget_s: float | None = None) -> list[int]:
    """Build packs for the most urgent relevant jobs. Returns the job ids that got a new pack."""
    rx = rankonex()
    if not rx:
        return []
    code, prof = rx
    min_score = float(settings.get("resume", "min_score", settings.get("scoring", "notify_threshold", 7.0)))
    per_run = int(settings.get("packs", "per_run", 3))
    budget = budget_s if budget_s is not None else float(settings.get("packs", "budget_seconds", 600))
    # only NEW jobs (first seen in the last `fresh_hours`): the point is applying within minutes, and building packs for
    # the whole backlog would slow every scan (later alerts) and flood you with "resume ready" for old jobs.
    # Newest first, then best score; a job whose posting changed (or proxy → real text) gets rebuilt.
    fresh = float(settings.get("packs", "fresh_hours", 48))
    rows = db.q("""SELECT j.*, s.score FROM jobs j JOIN scores s ON s.job_id=j.id LEFT JOIN packs p ON p.job_id=j.id
        WHERE j.status='active' AND j.duplicate_of IS NULL AND s.stage_a_pass=1 AND s.score >= ? AND s.hard_disqualifiers IN ('[]','')
          AND j.id NOT IN (SELECT job_id FROM applications) AND j.first_seen_at >= ?
          AND (p.job_id IS NULL OR p.content_hash != COALESCE(j.content_hash,'') OR (p.status='failed' AND p.created_at < ?))
        ORDER BY j.first_seen_at DESC, s.score DESC LIMIT ?""",
                (min_score, _hours_ago(fresh), _hours_ago(6), per_run))
    out_root = root / settings.get("packs", "output_dir", "output/packs")
    cv_dir = root / settings.get("resume", "output_dir", "output/resumes")
    cv_dir.mkdir(parents=True, exist_ok=True)
    t0, done = time.monotonic(), []
    for j in rows:
        if time.monotonic() - t0 > budget:
            log.info("apply packs: time budget used – the rest are built on the next scan")
            break
        text, proxy = _job_text(db, j, code)
        rec = {"job_id": j["id"], "content_hash": j.get("content_hash") or "", "proxy": int(proxy), "created_at": now_iso()}
        if not text:
            db.upsert("packs", {**rec, "status": "failed", "error": "no description yet (and too few earlier postings with this title)"}, ["job_id"])
            continue
        folder = out_root / str(j["id"])
        shutil.rmtree(folder, ignore_errors=True)
        job_file = Path(tempfile.mkdtemp(prefix="job_")) / "job.txt"
        job_file.write_text(text, encoding="utf-8")
        depth = settings.get("packs", "depth", "deep")
        try:
            p = _run(code, ["pack", "--profile", str(prof), "--job", str(job_file), "--out", str(folder), "--url", j.get("url") or "",
                            "--depth", depth], int(settings.get("packs", "timeout_seconds", 420)))
            if p.returncode != 0:
                raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1][:300] if (p.stderr or p.stdout).strip() else f"exit {p.returncode}")
            pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
        except Exception as e:  # never let one pack break the scan
            log.warning(f"apply pack failed for job {j['id']}: {type(e).__name__}")
            db.upsert("packs", {**rec, "status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}, ["job_id"])
            continue
        before, after = pack.get("rank_before") or {}, pack.get("rank_after") or {}
        db.upsert("packs", {**rec, "status": "ready", "depth": depth, "folder": folder.relative_to(root).as_posix(),
                            "top1_before": before.get("top1_rate"), "top1_after": after.get("top1_rate"),
                            "median_after": after.get("median_rank"), "pool": after.get("pool_size"),
                            "pack": json.dumps({k: pack.get(k) for k in ("cover_letter", "form", "screening_answers", "gaps_for_later", "bullets_added")}),
                            "error": None}, ["job_id"])
        _use_as_cv(db, j, Path(pack["resume_pdf"]), cv_dir)
        done.append(j["id"])
    db.commit()
    return done


def _use_as_cv(db, job: dict, pdf: Path, cv_dir: Path) -> None:
    """The pack's resume becomes this job's tailored CV (dashboard link, encrypted site, Telegram attachment)."""
    r = db.one("SELECT file FROM resumes WHERE job_id=?", (job["id"],))
    name = r["file"] if r and r["file"] else cv_filename(pdf.stem.replace("_Resume", ""), job)
    shutil.copyfile(pdf, cv_dir / name)
    if not r:
        db.upsert("resumes", {"job_id": job["id"], "content_hash": job.get("content_hash") or "", "file": name, "created_at": now_iso()}, ["job_id"])


def keep_pack_cvs(db, root: Path, settings) -> None:
    """build_resumes may rewrite a CV (e.g. after a skill change); put the RankOne X version back.
    Also drops packs of jobs that closed without an application, so the vault doesn't grow forever."""
    cv_dir = root / settings.get("resume", "output_dir", "output/resumes")
    for r in db.q("""SELECT p.job_id, p.folder FROM packs p JOIN jobs j ON j.id=p.job_id
                     WHERE j.status!='active' AND p.job_id NOT IN (SELECT job_id FROM applications)"""):
        if r["folder"]:
            shutil.rmtree(root / r["folder"], ignore_errors=True)
        db.execute("DELETE FROM packs WHERE job_id=?", (r["job_id"],))
    for r in db.q("SELECT p.folder, r.file FROM packs p JOIN resumes r ON r.job_id=p.job_id WHERE p.status='ready'"):
        folder = root / r["folder"] if r["folder"] else None
        src = next(folder.glob("*_Resume.pdf"), None) if folder and folder.exists() else None
        dst = cv_dir / r["file"] if r["file"] else None
        if src and dst and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
            shutil.copyfile(src, dst)
    db.commit()


def _hours_ago(h: float) -> str:
    from datetime import timedelta

    from ..util import iso, utcnow
    return iso(utcnow() - timedelta(hours=h))

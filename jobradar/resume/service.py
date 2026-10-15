"""Generate a tailored CV (PDF) for every relevant job and remember it in the `resumes` table."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..util import log, now_iso
from .tailor import load_master, tailor

_WARNED = False


def _safe(s: str, n: int) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:n] or "role"


def cv_filename(cv_name: str, job: dict) -> str:
    company = re.sub(r"\(.*?\)", "", job.get("company_display") or "")
    return f"{_safe(cv_name, 30)}_{_safe(company, 30)}_{_safe(job.get('title'), 50)}_{job['id']}.pdf"


def build_resumes(db, settings, root: Path, job_ids: list[int] | None = None) -> int:
    """Create/refresh CVs for relevant active jobs (or exactly `job_ids`). Returns how many PDFs were written."""
    global _WARNED
    if not settings.get("resume", "enabled", True) and not job_ids:
        return 0
    if not (root / "config" / "resume.json").exists():
        return 0
    try:
        from .render import render_pdf
        import reportlab  # noqa: F401
    except ImportError:
        if not _WARNED:
            log.warning("tailored CVs skipped – the 'reportlab' package is missing (run: python -m pip install reportlab)")
            _WARNED = True
        return 0
    master = load_master(root)
    mh = hashlib.sha256(json.dumps(master, sort_keys=True).encode()).hexdigest()[:10]  # editing resume.json → rebuild
    prof = settings.profile.get("skills", {})
    out_dir = root / settings.get("resume", "output_dir", "output/resumes")
    out_dir.mkdir(parents=True, exist_ok=True)
    min_score = float(settings.get("resume", "min_score", settings.get("scoring", "notify_threshold", 7.0)))
    if job_ids:
        rows = db.q(f"SELECT j.* FROM jobs j WHERE j.id IN ({','.join('?' * len(job_ids))})", job_ids)
    else:
        rows = db.q("""SELECT j.* FROM jobs j JOIN scores s ON s.job_id=j.id LEFT JOIN resumes r ON r.job_id=j.id
            WHERE j.status='active' AND j.duplicate_of IS NULL AND s.stage_a_pass=1 AND s.score >= ?
              AND s.hard_disqualifiers IN ('[]','') AND (r.job_id IS NULL OR r.content_hash != COALESCE(j.content_hash, '') || ':' || ?)""", (min_score, mh))
    n = 0
    for j in rows:
        cv, rep = tailor(master, j, prof)
        name = cv_filename(master["name"], j)
        try:
            (out_dir / name).write_bytes(render_pdf(cv, subject=f"{j.get('title')} – {j.get('company_display')}"))
        except Exception as e:  # never let one CV break the scan
            log.warning(f"tailored CV failed for job {j['id']}: {type(e).__name__}")
            continue
        db.upsert("resumes", {"job_id": j["id"], "content_hash": f"{j.get('content_hash') or ''}:{mh}", "file": name,
                              "coverage_before": rep["coverage_before"], "coverage_after": rep["coverage_after"],
                              "keywords_total": rep["keywords_total"], "matched": rep["matched"],
                              "added": rep["added"], "missing": rep["missing"],
                              "created_at": now_iso()}, ["job_id"])
        n += 1
    db.commit()
    prune_resumes(db, out_dir)
    return n


def prune_resumes(db, out_dir: Path) -> int:
    """Delete CVs of jobs that are closed (unless you applied with them) so the folder doesn't grow forever."""
    keep = {r["file"] for r in db.q("""SELECT r.file FROM resumes r JOIN jobs j ON j.id=r.job_id
              WHERE j.status='active' OR r.job_id IN (SELECT job_id FROM applications)""")}
    keep |= {r["cv_file"] for r in db.q("SELECT cv_file FROM applications WHERE cv_file IS NOT NULL")}
    gone = 0
    for f in Path(out_dir).glob("*.pdf"):
        if f.name not in keep:
            f.unlink()
            gone += 1
    db.execute("DELETE FROM resumes WHERE file NOT IN (%s)" % ",".join("?" * len(keep)) if keep else "DELETE FROM resumes", tuple(keep))
    db.commit()
    return gone

"""Jobs you have applied to – stored in the `applications` table so they survive every rescan.

A new posting counts as "already applied" when it is the same job, or the same role at the same employer
(same fingerprint, or ≥ 85 % title overlap) – e.g. a repost, or the LinkedIn copy of a job you applied to on the
company site. Those jobs are flagged on the dashboard and never trigger another alert.
"""
from __future__ import annotations

from .db import Database
from .util import jaccard, normalize_company, normalize_title, now_iso

STATUSES = ("applied", "interview", "offer", "rejected", "withdrawn")


def all_applications(db: Database) -> list[dict]:
    return db.q("""SELECT a.*, j.status AS posting_status, j.url AS current_url,
        (SELECT s.score FROM scores s WHERE s.job_id=a.job_id) AS current_score
        FROM applications a LEFT JOIN jobs j ON j.id=a.job_id ORDER BY a.applied_at DESC""")


def match(job: dict, apps: list[dict]) -> dict | None:
    """The application this job belongs to (itself, a repost, or another source's copy), else None."""
    nt = job.get("norm_title") or normalize_title(job.get("title") or "")
    co = normalize_company(job.get("company_display") or "")
    for a in apps:
        if a["job_id"] == job.get("id"):
            return a
    for a in apps:
        same_co = (a.get("company_id") and a["company_id"] == job.get("company_id")) or \
            (co and normalize_company(a.get("company") or "") == co)
        if same_co and ((a.get("fingerprint") and a["fingerprint"] == job.get("fingerprint")) or
                        jaccard(a.get("norm_title") or "", nt) >= 0.85):
            return a
    return None


def set_applied(db: Database, job_id: int, applied: bool, status: str | None = None, note: str | None = None) -> dict:
    if not applied:
        db.execute("DELETE FROM applications WHERE job_id=?", (job_id,))
        db.commit()
        return {"job_id": job_id, "applied": False}
    j = db.one("""SELECT j.*, s.score, r.file AS cv_file FROM jobs j LEFT JOIN scores s ON s.job_id=j.id
                  LEFT JOIN resumes r ON r.job_id=j.id WHERE j.id=?""", (job_id,))
    if not j:
        raise KeyError(job_id)
    old = db.one("SELECT * FROM applications WHERE job_id=?", (job_id,)) or {}
    st = status if status in STATUSES else old.get("status") or "applied"
    db.upsert("applications", {
        "job_id": job_id, "company_id": j["company_id"], "company": j["company_display"], "title": j["title"],
        "norm_title": j.get("norm_title") or normalize_title(j["title"] or ""), "fingerprint": j.get("fingerprint"),
        "url": j["url"], "city": j.get("city"), "score": j.get("score"), "cv_file": j.get("cv_file"), "status": st,
        "note": note if note is not None else old.get("note"),
        "applied_at": old.get("applied_at") or now_iso(), "updated_at": now_iso()}, ["job_id"])
    db.commit()
    return {"job_id": job_id, "applied": True, "status": st}


# ---------------------------------------------------------------- "Not relevant"
# Jobs you dismissed on the dashboard. Kept in the kv table (survives every rescan). A repost or another source's copy
# of the same role at the same employer counts as dismissed too (same rule as "already applied"), so it stays hidden
# and never sends a Telegram alert.
NR_KEY = "not_relevant"


def not_relevant(db: Database) -> list[dict]:
    return db.kv_get(NR_KEY, []) or []


def set_not_relevant(db: Database, job_id: int, on: bool) -> dict:
    lst = [x for x in not_relevant(db) if x.get("job_id") != job_id]
    if on:
        j = db.one("SELECT id, company_id, company_display, title, norm_title, fingerprint FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise KeyError(job_id)
        lst.append({"job_id": job_id, "company_id": j["company_id"], "company": j["company_display"], "title": j["title"],
                    "norm_title": j.get("norm_title") or normalize_title(j["title"] or ""),
                    "fingerprint": j.get("fingerprint"), "at": now_iso()})
    db.kv_set(NR_KEY, lst[-3000:])
    db.commit()
    return {"job_id": job_id, "not_relevant": bool(on)}

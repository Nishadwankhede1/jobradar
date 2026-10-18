"""Monitoring pipeline: due sources → fetch → normalise → upsert/dedup → score → notify → record health."""
from __future__ import annotations

import json
import os

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from . import adapters as _adapters  # noqa: F401  (registers adapters)
from .adapters.base import AdapterConfigError, get_adapter
from .config import Settings, load_companies
from .db import Database
from .external import sources as _ext  # noqa: F401  (registers external adapters)
from .external.email_alerts import fetch_email_leads
from .http import HttpClient, HttpError, NotModified
from .location import classify
from .models import AdapterResult, FetchContext, RawJob
from .notify.service import Notifier
from .scoring.llm import LLMAssessor
from .scoring.scorer import detect_agency, score_job
from .util import iso, jaccard, log, normalize_company, normalize_title, now_iso, parse_dt, redact, sha, slugify, utcnow

AUTOMATABLE_EXCLUDE = {"manual", None, ""}


# ------------------------------------------------------------------------------------------------ companies
def sync_companies(db: Database, settings: Settings, companies: list[dict] | None = None) -> int:
    full = companies is None
    companies = companies if companies is not None else load_companies(settings)
    pol = settings.raw["polling"]
    now = now_iso()
    for c in companies:
        cid = c.get("id") or slugify(c["name"])
        exists = db.one("SELECT created_at FROM companies WHERE id=?", (cid,))
        db.upsert("companies", {
            "id": cid, "name": c["name"], "industry": c.get("industry"), "website": c.get("website"),
            "careers_url": c.get("careers_url"), "ats": c.get("ats"), "ats_config": c.get("ats_config") or {},
            "dubai_presence": c.get("dubai_presence"), "job_families": c.get("job_families") or [],
            "priority": int(c.get("priority") or 3), "verified": 1 if c.get("verified") else 0,
            "verification_note": c.get("verification_note"), "hires_evidence": c.get("hires_analytics_evidence"),
            "kind": c.get("kind", "employer"), "enabled": 0 if c.get("enabled") is False else 1,
            "created_at": exists["created_at"] if exists else now, "updated_at": now}, ["id"])
        base = pol["aggregator"] if c.get("kind") == "aggregator" else pol.get(f"priority_{int(c.get('priority') or 3)}", 60)
        src = db.one("SELECT last_status FROM sources WHERE company_id=?", (cid,))
        status = "unmonitored" if c.get("ats") in AUTOMATABLE_EXCLUDE else None
        if not src:
            db.upsert("sources", {"company_id": cid, "interval_minutes": base, "next_due_at": None,
                                  "last_status": status}, ["company_id"])
        elif (src["last_status"] == "unmonitored") != (status == "unmonitored"):
            # the employer gained (or lost) an automatable feed → reset so the next scan baselines it
            db.update("sources", {"company_id": cid}, {"last_status": status, "next_due_at": None, "baseline_done": 0,
                                                       "consecutive_failures": 0})
    if full:  # employers removed from config (merged into another, or dropped) stop being polled
        ids = {c.get("id") or slugify(c["name"]) for c in companies}
        for row in db.q("SELECT id FROM companies WHERE kind='employer' AND enabled=1"):
            if row["id"] not in ids:
                db.update("companies", {"id": row["id"]}, {"enabled": 0, "updated_at": now})
        merge_duplicate_leads(db)
    db.commit()
    return len(companies)


def promote_lead_employers(db: Database, limit: int) -> list[str]:
    """Employers we only know from leads (LinkedIn / e-mail alerts, job-board searches) → start reading their own careers
    feed with the 'auto' adapter, so no employer that posts relevant jobs is left unwatched. Busiest first, agencies never,
    at most `limit` per scan (a feed that isn't found is re-probed weekly, so this stays light)."""
    if limit <= 0:
        return []
    rows = db.q("""SELECT c.id, c.name, COUNT(j.id) AS n, MAX(s.score) AS best FROM companies c
        JOIN jobs j ON j.company_id = c.id AND j.status = 'active' AND j.duplicate_of IS NULL AND COALESCE(j.is_agency, 0) = 0
        LEFT JOIN scores s ON s.job_id = j.id
        WHERE c.kind = 'lead' AND c.enabled = 1 AND COALESCE(c.ats, 'manual') = 'manual'
        GROUP BY c.id ORDER BY n DESC, best DESC LIMIT ?""", (limit,))
    now = now_iso()
    for r in rows:
        db.update("companies", {"id": r["id"]}, {"ats": "auto", "ats_config": {}, "updated_at": now,
                  "verification_note": "Found through job leads (LinkedIn / e-mail alerts, job boards) – now reading its own "
                                       "careers feed when it has a public one (re-checked weekly if not)"})
        db.update("sources", {"company_id": r["id"]}, {"last_status": None, "next_due_at": None, "baseline_done": 0,
                                                     "consecutive_failures": 0})
    if rows:
        db.commit()
        log.info("now following %d employer(s) found through leads: %s", len(rows), ", ".join(r["name"] for r in rows))
    return [r["name"] for r in rows]


def merge_duplicate_leads(db: Database) -> int:
    """Auto-created lead employers that differ only by spelling (accents, apostrophes) → fold into one."""
    known = {}
    for c in db.q("SELECT id, name, kind FROM companies WHERE kind != 'aggregator' AND enabled=1 "
                  "ORDER BY CASE kind WHEN 'employer' THEN 0 ELSE 1 END, id"):
        known.setdefault(normalize_company(c["name"]), c["id"])
    merged = 0
    for c in db.q("SELECT id, name FROM companies WHERE kind='lead' AND enabled=1"):
        target = known.get(normalize_company(c["name"]))
        if target and target != c["id"]:
            moved = [r["id"] for r in db.q("SELECT id FROM jobs WHERE company_id=? AND status='active'", (c["id"],))]
            db.execute("UPDATE OR IGNORE jobs SET company_id=? WHERE company_id=?", (target, c["id"]))
            db.update("companies", {"id": c["id"]}, {"enabled": 0, "updated_at": now_iso()})
            dedup(db, moved)
            merged += 1
    return merged


_ALIASES: dict | None = None


def company_aliases() -> dict:
    """normalised alias → company id, from config/company_aliases.json plus every employer's `brands` list."""
    global _ALIASES
    if _ALIASES is None:
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        out = {}
        try:
            for cid, names in json.loads((root / "config" / "company_aliases.json").read_text(encoding="utf-8")).items():
                out.update({normalize_company(n): cid for n in names})
        except (OSError, ValueError):
            pass
        try:
            for c in json.loads((root / "config" / "companies.json").read_text(encoding="utf-8")):
                out.update({normalize_company(b): c["id"] for b in c.get("brands") or [] if normalize_company(b)})
        except (OSError, ValueError, KeyError):
            pass
        _ALIASES = out
    return _ALIASES


def resolve_company(db: Database, name: str | None, fallback_id: str) -> tuple[str, str]:
    """Map a company name from an aggregator/e-mail lead onto the employer DB (creating a 'lead' employer)."""
    if not name:
        return fallback_id, "Unknown employer"
    n = normalize_company(name)
    alias = company_aliases().get(n)
    if alias and db.one("SELECT 1 FROM companies WHERE id=?", (alias,)):
        return alias, db.scalar("SELECT name FROM companies WHERE id=?", (alias,))
    for c in db.q("SELECT id, name FROM companies WHERE kind != 'aggregator'"):
        # exact normalised match only – prefix matching wrongly merged e.g. "BlackStone eIT" into "Blackstone"
        if normalize_company(c["name"]) == n:
            return c["id"], c["name"]
    cid = "lead-" + slugify(name)
    if not db.one("SELECT 1 FROM companies WHERE id=?", (cid,)):
        db.upsert("companies", {"id": cid, "name": name, "kind": "lead", "ats": "manual", "priority": 3, "enabled": 1,
                                "verification_note": "auto-discovered from a job lead", "created_at": now_iso(),
                                "updated_at": now_iso()}, ["id"])
        db.upsert("sources", {"company_id": cid, "last_status": "unmonitored"}, ["company_id"])
    return cid, name


# ------------------------------------------------------------------------------------------------ jobs
def upsert_job(db: Database, company: dict, rj: RawJob, scan_ts: str, baseline: bool) -> tuple[int, str]:
    """Insert or update one vacancy. Returns (job_id, change) where change ∈ new|updated|seen|reopened."""
    cid, display = company["id"], company["name"]
    fixed = (rj.extra or {}).get("company_id")
    if fixed and db.one("SELECT 1 FROM companies WHERE id=?", (fixed,)):
        cid = fixed
        display = db.scalar("SELECT name FROM companies WHERE id=?", (fixed,)) or rj.company_name or display
    elif company.get("kind") == "aggregator" or rj.source.startswith("email:"):
        cid, display = resolve_company(db, rj.company_name, company["id"])
    elif rj.company_name and rj.company_name.lower() not in display.lower():
        display = f"{rj.company_name} ({display})"
    existing = db.one("SELECT * FROM jobs WHERE company_id=? AND source=? AND external_id=?", (cid, rj.source, rj.external_id))
    title = rj.title or (existing or {}).get("title") or ""
    desc = rj.description if rj.description is not None else (existing or {}).get("description")
    loc = classify(rj.location or (existing or {}).get("location") or "", rj.country_code, desc or "")
    t_loc = classify(title)
    if t_loc["tier"] == "uae_other" and loc["tier"] in ("dubai", "uae_unspecified"):
        loc = t_loc  # e.g. "Business Development Manager - Abu Dhabi" listed under a Dubai office
    chash = sha(title, rj.location, (desc or "")[:20000])
    fp = sha(normalize_company(display), normalize_title(title), loc["city"])
    agency = rj.is_agency or detect_agency(desc or "", company)
    data = {"company_id": cid, "company_display": display, "source": rj.source, "external_id": rj.external_id,
            "title": title, "norm_title": normalize_title(title), "url": rj.url or (existing or {}).get("url"),
            "location": rj.location or (existing or {}).get("location"), "city": loc["city"], "location_tier": loc["tier"],
            "description": desc, "employment_type": rj.employment_type or (existing or {}).get("employment_type"),
            "department": rj.department or (existing or {}).get("department"),
            "posted_at": rj.posted_at or (existing or {}).get("posted_at"), "last_seen_at": scan_ts, "status": "active",
            "missing_count": 0, "fingerprint": fp, "is_official": 1 if rj.is_official else 0, "is_agency": 1 if agency else 0,
            "verification": "verified" if rj.is_official else "unverified",
            "evidence": f"listed by {rj.source} feed at {scan_ts}", "extra": rj.extra or {}}
    if rj.updated_marker is not None:
        data["updated_marker"] = rj.updated_marker
    if existing is None:
        data.update({"first_seen_at": scan_ts, "last_changed_at": scan_ts, "content_hash": chash, "baseline": 1 if baseline else 0})
        posted = parse_dt(rj.posted_at)
        if posted and not baseline and (posted.hour or posted.minute):
            data["latency_minutes"] = round(max(0.0, (parse_dt(scan_ts) - posted).total_seconds() / 60), 1)
        db.upsert("jobs", data, ["company_id", "source", "external_id"])
        jid = db.scalar("SELECT id FROM jobs WHERE company_id=? AND source=? AND external_id=?", (cid, rj.source, rj.external_id))
        db.event(jid, "created", f"baseline={baseline}")
        return jid, "new"
    change = "seen"
    if existing["status"] == "closed":
        change = "reopened"
        data["closed_at"] = None
        db.event(existing["id"], "reopened")
    if rj.description is not None and existing.get("content_hash") != chash:
        data["content_hash"], data["last_changed_at"] = chash, scan_ts
        if change == "seen":
            change = "updated"
        db.event(existing["id"], "updated", "title/location/description changed")
    db.update("jobs", {"id": existing["id"]}, data)
    return existing["id"], change


def mark_missing(db: Database, company_id: str, source: str, seen_ids: set[int], scan_ts: str, threshold: int) -> int:
    closed = 0
    for j in db.q("SELECT id, missing_count FROM jobs WHERE company_id=? AND source=? AND status='active'", (company_id, source)):
        if j["id"] in seen_ids:
            continue
        mc = (j["missing_count"] or 0) + 1
        if mc >= threshold:
            db.update("jobs", {"id": j["id"]}, {"missing_count": mc, "status": "closed", "closed_at": scan_ts,
                                                 "evidence": f"absent from {mc} consecutive complete scans (last {scan_ts})"})
            db.event(j["id"], "closed", f"missing {mc} scans")
            closed += 1
        else:
            db.update("jobs", {"id": j["id"]}, {"missing_count": mc})
    return closed


def dedup(db: Database, job_ids: list[int]):
    """Link cross-source duplicates & reposts. Canonical = official source, earliest seen."""
    for jid in job_ids:
        j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j or j["status"] != "active":
            continue
        peers = db.q("SELECT * FROM jobs WHERE id != ? AND status='active' AND (fingerprint=? OR (city=? AND company_id=?))",
                     (jid, j["fingerprint"], j["city"], j["company_id"]))
        peers = [p for p in peers if p["fingerprint"] == j["fingerprint"] or jaccard(p["norm_title"], j["norm_title"]) >= 0.85]
        if j["is_agency"] or not j["is_official"]:
            # a recruiter / job-board copy of an employer's own posting (different company name, same role & city)
            since = iso(utcnow() - timedelta(days=45))
            for p in db.q("SELECT * FROM jobs WHERE id != ? AND status='active' AND is_official=1 AND is_agency=0 AND city=? "
                          "AND company_id != ? AND first_seen_at >= ?", (jid, j["city"], j["company_id"], since)):
                if jaccard(p["norm_title"], j["norm_title"]) >= 0.9 and p["id"] not in {x["id"] for x in peers}:
                    peers.append(p)
        if peers:
            group = sorted(peers + [j], key=lambda x: (-int(x["is_official"] or 0), x["first_seen_at"] or "", x["id"]))
            canon = group[0]["id"]
            for g in group[1:]:
                if g["duplicate_of"] != canon:
                    db.update("jobs", {"id": g["id"]}, {"duplicate_of": canon})
                    db.event(g["id"], "duplicate", f"duplicate of job {canon}")
            if group[0]["duplicate_of"]:
                db.update("jobs", {"id": canon}, {"duplicate_of": None})
        if not j["reposted_from"]:
            since = iso(utcnow() - timedelta(days=90))
            prev = db.one("SELECT id FROM jobs WHERE fingerprint=? AND status='closed' AND id != ? AND closed_at >= ? "
                          "ORDER BY closed_at DESC LIMIT 1", (j["fingerprint"], jid, since))
            if prev and (j["first_seen_at"] or "") >= (db.scalar("SELECT closed_at FROM jobs WHERE id=?", (prev["id"],)) or ""):
                db.update("jobs", {"id": jid}, {"reposted_from": prev["id"]})
                db.event(jid, "reposted", f"same role closed earlier as job {prev['id']}")


# ------------------------------------------------------------------------------------------------ scoring
def score_pending(db: Database, settings: Settings, llm: LLMAssessor | None = None, rescore_all=False) -> int:
    from .scoring.scorer import SCORER_VERSION
    ver = db.one("SELECT value FROM kv WHERE key='scorer_version'")
    # rules changed since these jobs were scored → recheck jobs that passed the gate (the new rules only tighten)
    rules_changed = not rescore_all and (not ver or ver["value"] != SCORER_VERSION)
    where = "" if rescore_all else " AND (s.job_id IS NULL OR s.content_hash != j.content_hash" + \
        (" OR s.stage_a_pass = 1)" if rules_changed else ")")
    sql = ("SELECT j.*, s.content_hash AS scored_hash FROM jobs j LEFT JOIN scores s ON s.job_id = j.id "
           "WHERE j.status='active'" + where)
    n = 0
    for j in db.q(sql):
        company = db.one("SELECT * FROM companies WHERE id=?", (j["company_id"],)) or {"name": j["company_display"]}
        sc = score_job(j, company, settings)
        if llm and llm.enabled and sc["eligible"] and sc["stage_a_pass"] and sc["score"] >= float(settings.get("llm", "min_rule_score", 5.5)) \
                and len(j.get("description") or "") >= 300:
            res = llm.assess(j, j["company_display"], j["content_hash"])
            if res:
                sc = score_job(j, company, settings, llm_result=res)
        db.upsert("scores", {"job_id": j["id"], "content_hash": j["content_hash"], "score": sc["score"], "label": sc["label"],
                             "stage_a_pass": 1 if sc["stage_a_pass"] else 0, "stage_a_reason": sc["stage_a_reason"],
                             "hard_disqualifiers": sc["hard_disqualifiers"], "subscores": sc["subscores"],
                             "matched_skills": sc["matched_skills"], "missing_skills": sc["missing_skills"],
                             "reasons": sc["reasons"], "gaps": sc["gaps"], "uncertainties": sc["uncertainties"],
                             "recommendation": sc["recommendation"], "confidence": sc["confidence"], "method": sc["method"],
                             "visa": sc.get("visa"), "scored_at": now_iso()}, ["job_id"])
        n += 1
    db.upsert("kv", {"key": "scorer_version", "value": SCORER_VERSION}, ["key"])
    db.commit()
    return n


# ------------------------------------------------------------------------------------------------ scheduling
def next_interval(settings: Settings, company: dict, src: dict, changed: bool, ok: bool) -> int:
    pol = settings.raw["polling"]
    base = pol["aggregator"] if company.get("kind") == "aggregator" else pol.get(f"priority_{int(company.get('priority') or 3)}", 60)
    if not ok:
        fails = int(src.get("consecutive_failures") or 0) + 1
        return int(min(pol["max_backoff"], base * (2 ** min(fails, 6))))
    last_change = parse_dt(now_iso() if changed else src.get("last_change_at"))
    if last_change and utcnow() - last_change < timedelta(days=7):
        return int(max(pol["min"], base * 0.5))
    if not last_change or utcnow() - last_change > timedelta(days=14):
        return int(min(pol["max"], base * 1.5))
    return int(base)


# Errors produced by code that has since been fixed: sources stuck in back-off with one of these retry on the next scan
# instead of waiting out the back-off (self-healing after an update). Each entry: (ats, LIKE pattern on last_error).
HEALED_ERRORS = [("workday", "JSONDecodeError%"), ("%", "%run time budget exhausted%")]


def heal_sources(db: Database) -> int:
    n = 0
    for ats, pattern in HEALED_ERRORS:
        cur = db.execute("UPDATE sources SET next_due_at=NULL, last_status=CASE WHEN last_error LIKE '%run time budget exhausted%' "
                         "THEN 'ok' ELSE last_status END, consecutive_failures=CASE WHEN last_error LIKE '%run time budget exhausted%' "
                         "THEN 0 ELSE consecutive_failures END WHERE last_status='error' AND last_error LIKE ? "
                         "AND company_id IN (SELECT id FROM companies WHERE ats LIKE ?)", (pattern, ats))
        n += cur.rowcount or 0
    if n:
        db.commit()
        log.info("self-heal: %d source(s) with an already-fixed error retry now", n)
    return n


def due_sources(db: Database, only: list[str] | None = None, force=False) -> list[dict]:
    rows = db.q("SELECT c.*, s.next_due_at, s.baseline_done, s.consecutive_failures, s.last_change_at, s.total_runs, "
                "s.total_failures FROM companies c JOIN sources s ON s.company_id = c.id "
                "WHERE c.enabled = 1 AND c.ats IS NOT NULL AND c.ats != 'manual' ORDER BY c.priority, s.next_due_at")
    now = now_iso()
    from .adapters.browser import browser_enabled
    if not browser_enabled():  # headless-browser sources run only in the hourly browser workflow
        rows = [r for r in rows if r["ats"] != "browser"]
    if only:
        wanted = {o.lower() for o in only}
        rows = [r for r in rows if r["id"].lower() in wanted or r["name"].lower() in wanted or r["ats"] in wanted]
    return [r for r in rows if force or only or not r["next_due_at"] or r["next_due_at"] <= now]


# ------------------------------------------------------------------------------------------------ main entry
def run_scan(settings: Settings, db: Database, http: HttpClient, notifier: Notifier | None = None,
             only: list[str] | None = None, force=False, trigger="manual", include_email=True,
             companies: list[dict] | None = None) -> dict:
    t0 = time.monotonic()
    started = now_iso()
    sync_companies(db, settings, companies)
    if not only:
        promote_lead_employers(db, int(settings.get("sources", "promote_leads_per_scan", 0) or 0))
    if os.environ.get("JOBRADAR_TELEGRAM_COMMANDS") == "1":  # cloud: apply /applied_… taps before scoring and alerts
        from .notify.commands import process_commands
        process_commands(db, http)
    from .region import is_eu
    if is_eu():  # Europe instance: keep the official sponsor register current (monthly list, checked weekly)
        from .scoring.sponsor import refresh_registers
        log.info("NL sponsor register: %s", refresh_registers(http, db))
        from .region import enabled_countries
        if "nl" in enabled_countries() and not only:  # follow every Dutch sponsor that has a public job feed
            from .external.register_sweep import sweep
            sweep(db, http, "nl", int(settings.get("sources", "register_sweep_per_scan", 0) or 0),
                  int(settings.raw["polling"].get("priority_2", 60)))
    heal_sources(db)
    http.cache.update(db.load_http_cache())
    sources = due_sources(db, only, force)
    max_rt = float(settings.get("scan", "max_runtime_seconds", 420))
    workers = int(settings.get("scan", "workers", 8))
    threshold = int(settings.get("scan", "missing_scans_before_closed", 2))
    stats = {"sources_checked": 0, "sources_ok": 0, "sources_failed": 0, "sources_not_modified": 0, "jobs_seen": 0,
             "jobs_new": 0, "jobs_updated": 0, "jobs_closed": 0}
    touched: list[int] = []
    notes = []

    def task(company: dict, ctx: FetchContext):
        start = time.monotonic()
        http.set_source(company["id"])
        try:
            res = get_adapter(company["ats"], http).fetch(company, ctx)
            return company, ctx, res, None, int((time.monotonic() - start) * 1000)
        except NotModified:
            return company, ctx, AdapterResult([], complete=False, not_modified=True), None, int((time.monotonic() - start) * 1000)
        except Exception as e:  # never let one source kill the run
            return company, ctx, None, redact(f"{type(e).__name__}: {e}")[:400], int((time.monotonic() - start) * 1000)
        finally:
            http.set_source(None)

    def handle(company, ctx: FetchContext, res: AdapterResult | None, err: str | None, ms: int):
        cid = company["id"]
        pending = http.take_pending(cid)
        # a new job whose description could not be fetched (detail cap / budget) means the scan is not finished
        truncated = bool(res) and any(rj.description is None and rj.external_id not in ctx.known for rj in res.jobs)
        if res and not err and not truncated:
            http.cache.update(pending)  # ETags are only trusted after a fully successful source scan
        src = db.one("SELECT * FROM sources WHERE company_id=?", (cid,)) or {}
        scan_ts = now_iso()
        stats["sources_checked"] += 1
        baseline = not src.get("baseline_done")
        if err and "run time budget exhausted" in err:
            # not the employer's fault: this scan ran out of time before finishing it → simply due again next run
            stats["sources_checked"] -= 1
            db.update("sources", {"company_id": cid}, {"next_due_at": None})
            log.info("source %-28s deferred to next run (time budget)", company["name"][:28])
            return
        if err:
            stats["sources_failed"] += 1
            iv = next_interval(settings, company, src, False, False)
            db.update("sources", {"company_id": cid}, {
                "last_run_at": scan_ts, "last_status": "error", "last_error": err, "last_duration_ms": ms,
                "consecutive_failures": int(src.get("consecutive_failures") or 0) + 1,
                "total_runs": int(src.get("total_runs") or 0) + 1, "total_failures": int(src.get("total_failures") or 0) + 1,
                "interval_minutes": iv, "next_due_at": iso(utcnow() + timedelta(minutes=iv))})
            log.warning("source %-28s FAILED %s", company["name"][:28], err[:160])
            return
        changed = False
        if res.not_modified:
            stats["sources_not_modified"] += 1
            db.execute("UPDATE jobs SET last_seen_at=?, missing_count=0 WHERE company_id=? AND source=? AND status='active'",
                       (scan_ts, cid, company["ats"]))
            status = "not_modified"
        else:
            seen: set[int] = set()
            for rj in res.jobs:
                jid, change = upsert_job(db, company, rj, scan_ts, baseline)
                seen.add(jid)
                touched.append(jid)
                stats["jobs_seen"] += 1
                if change in ("new", "reopened"):
                    stats["jobs_new"] += 1
                    changed = True
                elif change == "updated":
                    stats["jobs_updated"] += 1
                    changed = True
            if res.complete and company.get("kind") != "aggregator":
                c = mark_missing(db, cid, company["ats"], seen, scan_ts, threshold)
                stats["jobs_closed"] += c
                changed = changed or c > 0
            status = "ok" if (res.complete or company.get("kind") == "aggregator") else "partial"
            if res.notes:
                notes.append(f"{company['name']}: {res.notes}")
        stats["sources_ok"] += 1
        iv = next_interval(settings, company, src, changed, True)
        upd = {"last_run_at": scan_ts, "last_success_at": scan_ts, "last_status": status, "last_error": res.notes or None,
               "consecutive_failures": 0, "last_job_count": len(res.jobs) if not res.not_modified else src.get("last_job_count"),
               "total_runs": int(src.get("total_runs") or 0) + 1, "interval_minutes": iv, "last_duration_ms": ms,
               "next_due_at": iso(utcnow() + timedelta(minutes=iv))}
        if res.state_out is not None:
            db.kv_set(f"adapter_state:{cid}", res.state_out)
        if res.baseline_ready and not res.not_modified and not truncated and (res.complete or res.jobs or company.get("kind") == "aggregator"):
            upd["baseline_done"] = 1  # never on 304 / empty-partial results → no later alert floods
        if truncated:
            upd["last_status"], upd["last_error"] = "partial", "some job details deferred to the next run"
        if changed:
            upd["last_change_at"] = scan_ts
        db.update("sources", {"company_id": cid}, upd)
        log.info("source %-28s %-12s %3d UAE jobs (%d ms)", company["name"][:28], status, len(res.jobs), ms)

    http.deadline = time.monotonic() + max_rt
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for company in sources:
            known = {r["external_id"]: r["updated_marker"] or "" for r in db.q(
                "SELECT external_id, updated_marker FROM jobs WHERE company_id=? AND source=? AND description IS NOT NULL",
                (company["id"], company["ats"]))}
            ctx = FetchContext(known=known, max_detail_fetches=int(settings.get("scan", "max_detail_fetches_per_source", 60)),
                               state=db.kv_get(f"adapter_state:{company['id']}", {}) or {})
            futures.append(pool.submit(task, company, ctx))
        deferred = 0
        for f in as_completed(futures):
            if time.monotonic() - t0 > max_rt:
                deferred = sum(1 for x in futures if x.cancel())
            if f.cancelled():
                continue
            handle(*f.result())
            db.commit()
        if deferred:
            notes.append(f"time budget reached – {deferred} sources deferred to next run")
    http.deadline = None

    if include_email:
        leads, msg = fetch_email_leads(db)
        notes.append(msg)
        if leads:
            agg = {"id": "email-alerts", "name": "Job-alert e-mails", "kind": "aggregator", "industry": "", "priority": 3}
            if not db.one("SELECT 1 FROM companies WHERE id='email-alerts'"):
                db.upsert("companies", {"id": "email-alerts", "name": agg["name"], "kind": "aggregator", "ats": "email",
                                        "enabled": 0, "created_at": now_iso(), "updated_at": now_iso()}, ["id"])
            for rj in leads:
                jid, change = upsert_job(db, agg, rj, now_iso(), False)
                touched.append(jid)
                stats["jobs_new"] += change == "new"

    # leads from aggregators / e-mails / manual imports can't be verified as closed → expire them by age
    stale = iso(utcnow() - timedelta(days=int(settings.get("scan", "lead_expiry_days", 21))))
    for j in db.q("SELECT id FROM jobs WHERE status='active' AND last_seen_at < ? AND (source IN ('workable_search','jooble','manual') "
                  "OR source LIKE 'email:%')", (stale,)):
        db.update("jobs", {"id": j["id"]}, {"status": "closed", "closed_at": now_iso(), "evidence": "lead not re-seen for 21+ days"})
        db.event(j["id"], "closed", "stale lead")
        stats["jobs_closed"] += 1
    dedup(db, list(dict.fromkeys(touched)))
    db.commit()
    llm = LLMAssessor(settings, http, db)
    scored = score_pending(db, settings, llm)
    try:  # tailored CV for every newly relevant job (needs reportlab + config/resume.json; skipped quietly otherwise)
        from .config import ROOT
        from .resume.service import build_resumes
        stats_cv = build_resumes(db, settings, ROOT)
    except Exception as e:
        log.warning(f"tailored CVs skipped: {type(e).__name__}: {e}")
        stats_cv = 0
    sent = failed = 0
    if notifier:
        sent, failed = notifier.process_alerts()
    db.save_http_cache(http.cache)
    run = {"started_at": started, "finished_at": now_iso(), "trigger": trigger, **stats, "alerts_sent": sent,
           "alerts_failed": failed, "http_requests": http.request_count,
           "notes": "; ".join(n for n in notes if n)[:2000]}
    db.execute(
        f"INSERT INTO scan_runs ({', '.join(run)}) VALUES ({', '.join('?' for _ in run)})", list(run.values()))
    db.commit()
    run["scored"] = scored
    run["tailored_cvs"] = stats_cv
    run["llm_calls"] = llm.calls
    run["duration_s"] = round(time.monotonic() - t0, 1)
    return run

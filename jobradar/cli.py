"""Command-line entry point:  python -m jobradar <command>"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from .config import ROOT, load_companies, load_settings
from .db import Database
from .http import HttpClient
from .models import RawJob
from .notify.channels import Telegram, build_channels
from .notify.service import Notifier
from .util import log, now_iso, setup_logging, slugify


def _ctx(args):
    settings = load_settings()
    db = Database(settings.path("database"))
    http = HttpClient(user_agent=settings.get("scan", "user_agent"),
                      min_interval=float(settings.get("scan", "per_host_min_interval", 0.6)))
    channels = build_channels(settings, http, dry_run=getattr(args, "dry_run", False), root=ROOT)
    return settings, db, http, Notifier(settings, db, channels)


def cmd_scan(args):
    from .dashboard import build_dashboard
    from .pipeline import run_scan
    settings, db, http, notifier = _ctx(args)
    run = run_scan(settings, db, http, notifier, only=args.only, force=args.force, trigger=args.trigger,
                   include_email=not args.no_email)
    d = notifier.maybe_send_digest()
    build_dashboard(settings, db)
    print(json.dumps({**run, "digests_sent": d}, indent=2))
    db.close()


def cmd_digest(args):
    settings, db, http, notifier = _ctx(args)
    print(f"digests sent: {notifier.maybe_send_digest(force=args.force)}")
    db.close()


def cmd_score(args):
    from .pipeline import score_pending, sync_companies
    from .scoring.llm import LLMAssessor
    settings, db, http, _ = _ctx(args)
    sync_companies(db, settings)
    print(f"scored {score_pending(db, settings, LLMAssessor(settings, http, db), rescore_all=args.all)} jobs")
    db.close()


def cmd_dashboard(args):
    from .dashboard import build_dashboard
    settings, db, _, _ = _ctx(args)
    print(build_dashboard(settings, db))
    db.close()


def cmd_stats(args):
    from .dashboard import coverage_stats
    settings, db, _, _ = _ctx(args)
    print(json.dumps(coverage_stats(db), indent=2))
    db.close()


def cmd_test_notify(args):
    settings, db, http, notifier = _ctx(args)
    if not notifier.channels:
        print("No channel configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see README).")
        sys.exit(1)
    for line in notifier.send_test():
        print(line)
    db.close()


def cmd_telegram_setup(args):
    settings = load_settings()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Set TELEGRAM_BOT_TOKEN first (create a bot with @BotFather).")
        sys.exit(1)
    os.environ.setdefault("TELEGRAM_CHAT_ID", "pending")
    t = Telegram(HttpClient(user_agent=settings.get("scan", "user_agent")))
    ups = t.get_updates()
    chats = {}
    for u in ups:
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    if not chats:
        print("No messages yet. Open your bot in Telegram, press Start / send 'hi', then rerun this command.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def cmd_import(args):
    """Import manually verified vacancies (JSON list) – e.g. the initial research shortlist."""
    from .pipeline import dedup, score_pending, sync_companies, upsert_job
    settings, db, http, _ = _ctx(args)
    sync_companies(db, settings)
    data = json.load(open(args.file, encoding="utf-8"))
    db.upsert("companies", {"id": "manual-import", "name": "Manual import", "kind": "aggregator", "ats": "manual",
                            "enabled": 0, "created_at": now_iso(), "updated_at": now_iso()}, ["id"])
    agg = {"id": "manual-import", "name": "Manual import", "kind": "aggregator", "industry": "", "priority": 3}
    ids = []
    for v in data:
        if args.only_open and not v.get("verified_open"):
            continue
        desc = v.get("description") or ""
        if desc.startswith("DESCRIPTION NOT RETRIEVED"):
            desc = ""
        url = v.get("official_url") or v.get("external_url") or ""
        rj = RawJob(source="manual", external_id=slugify(f"{v['company']}-{v['title']}")[:80], title=v["title"], url=url,
                    location=v.get("location") or "", description=desc or None, employment_type=v.get("employment_type"),
                    posted_at=v.get("posted_date"), company_name=v["company"], is_official=bool(v.get("official_url")),
                    is_agency=bool(v.get("is_agency")),
                    extra={"evidence": v.get("evidence"), "verified_at": v.get("verified_at"), "source_name": v.get("source_name")})
        jid, _ = upsert_job(db, agg, rj, now_iso(), baseline=True)
        db.update("jobs", {"id": jid}, {"evidence": f"{v.get('evidence', '')} (checked {v.get('verified_at')})"[:500],
                                        "verification": "verified" if v.get("verified_open") else "unverified"})
        ids.append(jid)
    dedup(db, ids)
    db.commit()
    print(f"imported {len(ids)} vacancies; scored {score_pending(db, settings)}")
    db.close()


def cmd_import_leads(args):
    """Import job leads read from LinkedIn search results (title + link only, not verified on the employer's site)."""
    from .pipeline import dedup, score_pending, sync_companies, upsert_job
    settings, db, http, _ = _ctx(args)
    sync_companies(db, settings)
    data = json.load(open(args.file, encoding="utf-8"))
    if not db.one("SELECT 1 FROM companies WHERE id='email-alerts'"):
        db.upsert("companies", {"id": "email-alerts", "name": "Job-alert e-mails", "kind": "aggregator", "ats": "email",
                                "enabled": 0, "created_at": now_iso(), "updated_at": now_iso()}, ["id"])
    agg = {"id": "email-alerts", "name": "Job-alert e-mails", "kind": "aggregator", "industry": "", "priority": 3}
    ids = []
    for v in data["leads"]:
        # same source/external_id as a LinkedIn alert e-mail, so a later alert for this job updates this row
        rj = RawJob(source="email:linkedin", external_id=str(v["id"]), title=v["title"],
                    url=f"https://www.linkedin.com/jobs/view/{v['id']}/", location=v.get("location") or "",
                    description=(f"[Key points noted from the LinkedIn description] {v['notes']}" if v.get("notes") else None),
                    company_name=v["company"], is_official=False,
                    extra={"via": "LinkedIn company search", "company_id": v.get("company_id"),
                           "checked_at": data.get("checked_at")})
        jid, _ = upsert_job(db, agg, rj, now_iso(), baseline=False)
        db.update("jobs", {"id": jid}, {"verification": "unverified",
                                        "evidence": f"seen on LinkedIn search results {data.get('checked_at')}"})
        ids.append(jid)
    dedup(db, ids)
    db.commit()
    print(f"imported {len(ids)} LinkedIn leads; scored {score_pending(db, settings)}")
    db.close()


def cmd_resume(args):
    from .dashboard import build_dashboard
    from .resume.service import build_resumes
    settings, db, http, _ = _ctx(args)
    ids = [int(x) for x in args.job] if args.job else None
    n = build_resumes(db, settings, ROOT, job_ids=ids)
    for r in db.q("SELECT r.*, j.title, j.company_display FROM resumes r JOIN jobs j ON j.id=r.job_id "
                  + ("WHERE r.job_id IN (%s) " % ",".join("?" * len(ids)) if ids else "") + "ORDER BY r.created_at DESC LIMIT 20",
                  ids or ()):
        print(f"{r['coverage_before']:>3}% → {r['coverage_after']:>3}%  {r['title'][:50]} @ {r['company_display'][:30]}  "
              f"output/resumes/{r['file']}")
    print(f"{n} tailored CV(s) written to {settings.get('resume', 'output_dir', 'output/resumes')}")
    build_dashboard(settings, db)
    db.close()


def cmd_cloud_restore(args):
    from .cloud import vault_restore
    try:
        print("vault restored" if vault_restore(Path(args.file)) else "no vault yet - starting fresh")
    except Exception as e:  # e.g. the key was changed on purpose → the old vault can't be opened any more
        print(f"::warning::saved state could not be decrypted ({type(e).__name__}) - was JOBRADAR_KEY changed? "
              "Starting with a fresh database.")


def cmd_cloud_save(args):
    from .cloud import vault_save
    print(f"vault saved ({vault_save(Path(args.file)) // 1024} KB, encrypted)")


def cmd_cloud_publish(args):
    from .cloud import publish
    from .notify.commands import bot_username
    settings, db, http, _ = _ctx(args)
    print(json.dumps(publish(Path(args.dir), settings, db, bot=bot_username(http))))
    db.close()


def cmd_skill(args):
    """Confirm (or remove) skills you have, so tailored CVs may use them: python -m jobradar skill add Airflow dbt"""
    from .resume.myskills import get_user_skills, set_user_skill
    settings, db, http, _ = _ctx(args)
    for t in args.terms or []:
        set_user_skill(db, t, args.action == "add")
    print("Your confirmed skills:", ", ".join(x["term"] for x in get_user_skills(db)) or "(none)")
    if args.terms:
        print("Run 'python -m jobradar resume' (or the next scan) to rebuild your CVs.")
    db.close()


def cmd_cloud_ticks(args):
    from .cloud import apply_ticks
    settings, db, http, _ = _ctx(args)
    print(f"ticks applied: {apply_ticks(Path(args.file), db)}")
    db.close()


def cmd_export(args):
    settings, db, _, _ = _ctx(args)
    rows = db.q("SELECT j.company_display, j.title, j.city, j.url, j.source, j.verification, j.first_seen_at, j.posted_at, "
                "s.score, s.label, s.recommendation, s.reasons, s.gaps, s.hard_disqualifiers FROM jobs j JOIN scores s "
                "ON s.job_id=j.id WHERE j.status='active' AND j.duplicate_of IS NULL ORDER BY s.score DESC")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow({k: "; ".join(v) if isinstance(v, list) else v for k, v in r.items()})
    print(f"wrote {len(rows)} rows → {out}")
    db.close()


def cmd_daemon(args):
    """Long-running scheduler for Docker / a VM / Windows: wakes every N minutes and scans whatever is due."""
    from .dashboard import build_dashboard
    from .pipeline import run_scan
    interval = int(args.tick)
    log.info("daemon started – tick every %d min", interval)
    while True:
        try:
            settings, db, http, notifier = _ctx(args)
            run = run_scan(settings, db, http, notifier, trigger="daemon")
            notifier.maybe_send_digest()
            build_dashboard(settings, db)
            log.info("scan done: %s", {k: run[k] for k in ("sources_checked", "sources_failed", "jobs_new", "alerts_sent")})
            db.close()
        except Exception as e:  # keep the daemon alive
            log.exception("scan crashed: %s", type(e).__name__)
        time.sleep(interval * 60)


def cmd_serve(args):
    from .server import serve
    serve(args.port, open_browser=args.open)


def cmd_add_company(args):
    settings = load_settings()
    path = settings.path("companies")
    companies = load_companies(settings)
    entry = {"id": slugify(args.name), "name": args.name, "industry": args.industry, "careers_url": args.careers_url,
             "ats": args.ats, "ats_config": json.loads(args.config or "{}"), "priority": args.priority,
             "verified": False, "verification_note": "added via CLI"}
    companies = [c for c in companies if c["id"] != entry["id"]] + [entry]
    path.write_text(json.dumps(companies, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"added {entry['id']} ({len(companies)} companies)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobradar", description="Dubai BI/analytics job discovery & monitoring")
    p.add_argument("--log", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan due sources, score, notify, rebuild dashboard")
    s.add_argument("--force", action="store_true", help="ignore schedules – scan every source now")
    s.add_argument("--only", nargs="*", help="company ids/names or ATS names")
    s.add_argument("--dry-run", action="store_true", help="write notifications to state/outbox.log instead of sending")
    s.add_argument("--no-email", action="store_true")
    s.add_argument("--trigger", default="manual")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("digest"); s.add_argument("--force", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_digest)
    s = sub.add_parser("score"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_score)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("test-notify"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_test_notify)
    sub.add_parser("telegram-setup").set_defaults(fn=cmd_telegram_setup)
    s = sub.add_parser("import-vacancies"); s.add_argument("file"); s.add_argument("--only-open", action="store_true"); s.set_defaults(fn=cmd_import)
    s = sub.add_parser("import-leads"); s.add_argument("file"); s.set_defaults(fn=cmd_import_leads)
    s = sub.add_parser("resume", help="build tailored CVs (all relevant jobs, or --job ID ...)")
    s.add_argument("--job", nargs="*", help="job id(s) from the dashboard"); s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("cloud-restore"); s.add_argument("file"); s.set_defaults(fn=cmd_cloud_restore)
    s = sub.add_parser("cloud-save"); s.add_argument("file"); s.set_defaults(fn=cmd_cloud_save)
    s = sub.add_parser("cloud-publish"); s.add_argument("dir"); s.set_defaults(fn=cmd_cloud_publish)
    s = sub.add_parser("skill", help="add/remove skills you confirm for tailored CVs")
    s.add_argument("action", choices=["add", "remove", "list"]); s.add_argument("terms", nargs="*"); s.set_defaults(fn=cmd_skill)
    s = sub.add_parser("cloud-ticks"); s.add_argument("file"); s.set_defaults(fn=cmd_cloud_ticks)
    s = sub.add_parser("export"); s.add_argument("--out", default="site/matches.csv"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("daemon"); s.add_argument("--tick", default=5); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_daemon)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--open", action="store_true"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("add-company")
    for a in ("--name", "--ats"):
        s.add_argument(a, required=True)
    s.add_argument("--industry", default=""); s.add_argument("--careers-url", default=""); s.add_argument("--config", default="{}")
    s.add_argument("--priority", type=int, default=2); s.set_defaults(fn=cmd_add_company)
    args = p.parse_args(argv)
    setup_logging(args.log)
    args.fn(args)


if __name__ == "__main__":
    main()

"""Message templates (Telegram HTML + plain-text fallback)."""
from __future__ import annotations

import html
import os
from datetime import datetime, timedelta, timezone

from ..region import COUNTRY_FLAGS, COUNTRY_NAMES, is_eu, place_label
from ..util import parse_dt

DUBAI = timezone(timedelta(hours=4))


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _dubai(ts: str | None, with_time=True) -> str:
    """Local time of the watched market: Dubai (GST) or, for the Europe instance, Amsterdam (CET/CEST)."""
    dt = parse_dt(ts)
    if not dt:
        return ""
    if is_eu():
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d %b %Y %H:%M %Z" if with_time else "%d %b %Y")
    return dt.astimezone(DUBAI).strftime("%d %b %Y %H:%M GST" if with_time else "%d %b %Y")


def eu_country(job: dict) -> str:
    """'nl' | 'de' | 'uk' for a Europe job (from its location), '' otherwise."""
    if not is_eu():
        return ""
    from ..location import classify
    return classify(job.get("location") or job.get("city") or "").get("country") or ""


def visa_line(v: dict | None) -> str:
    """One line summarising the sponsor badges (Europe instance)."""
    if not v:
        return ""
    bits = []
    if v.get("register"):
        bits.append(f"✅ official IND sponsor ({v['register']})")
    if v.get("ad") == "offers":
        bits.append("✅ ad offers visa sponsorship")
    elif v.get("ad") == "refuses":
        bits.append("⛔ ad says no sponsorship")
    if v.get("relocation"):
        bits.append("✈️ relocation support")
    if v.get("salary_ok") is False:
        bits.append(f"⚠️ pay ~€{v['salary_month']:,}/month is below the visa minimum (€{v['salary_floor']:,})")
    elif v.get("salary_ok"):
        bits.append(f"pay ~€{v['salary_month']:,}/month meets the visa minimum")
    return " · ".join(bits) or "no sponsor signal yet – ask the recruiter before applying"


def employment_label(job: dict) -> str:
    t = f"{job.get('employment_type') or ''} {job.get('title') or ''}".lower()
    if "intern" in t:
        return "Internship"
    if any(k in t for k in ("graduate", "trainee", "programme", "program")):
        return "Graduate / Trainee programme"
    if any(k in t for k in ("contract", "temporary", "fixed")):
        return "Contract"
    if "part" in t:
        return "Part-time"
    if job.get("employment_type"):
        return str(job["employment_type"]).replace("_", " ").title()
    return "Full-time (not stated – assumed)"


def source_label(job: dict) -> str:
    src = job.get("source", "")
    if src.startswith("email:"):
        return f"{src.split(':', 1)[1].title()} job alert (unverified lead)"
    if src == "jooble":
        return "Jooble aggregator (unverified lead)"
    names = {"greenhouse": "Greenhouse", "lever": "Lever", "workday": "Workday", "smartrecruiters": "SmartRecruiters",
             "ashby": "Ashby", "workable": "Workable", "workable_search": "Workable", "oracle_hcm": "Oracle Recruiting",
             "successfactors": "SuccessFactors", "eightfold": "Eightfold", "amazon": "amazon.jobs", "jsonld": "company site",
             "teamtailor": "Teamtailor", "personio": "Personio", "recruitee": "Recruitee", "bamboohr": "BambooHR",
             "pinpoint": "Pinpoint", "manual": "manual entry"}
    return f"Official Careers Website ({names.get(src, src)})"


def posted_label(job: dict) -> str:
    if job.get("posted_at"):
        return f"{_dubai(job['posted_at'], with_time=False)} (published by source)"
    return f"not published by source – first detected {_dubai(job.get('first_seen_at'))}"


def location_label(job: dict) -> str:
    city = job.get("city") or ""
    if is_eu():
        c = eu_country(job)
        name = COUNTRY_NAMES.get(c, "")
        return f"{city}, {name}" if city and name and city != name else (name or job.get("location") or city)
    if city in ("Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Fujairah"):
        return f"{city}, UAE"
    return job.get("location") or "UAE"


def job_alert(job: dict, sc: dict, kind: str = "alert") -> tuple[str, str]:
    header = {"alert": "NEW JOB FOUND", "update": "JOB UPDATED", "repost": "JOB REPOSTED"}.get(kind, "NEW JOB FOUND")
    if is_eu():
        header = f"{COUNTRY_FLAGS.get(eu_country(job), '🇪🇺')} {header}"
    if not job.get("is_official") and len(job.get("description") or "") < 200:
        header += " – TITLE ONLY (open the posting to check the details)"
    why = sc.get("reasons") or []
    gaps = sc.get("gaps") or []
    unc = sc.get("uncertainties") or []
    lines = [
        (f"<b>{header}</b>", header), ("", ""),
        (f"<b>Company:</b> {_e(job.get('company_display'))}", f"Company: {job.get('company_display')}"),
        (f"<b>Position:</b> {_e(job.get('title'))}", f"Position: {job.get('title')}"),
        (f"<b>Location:</b> {_e(location_label(job))}", f"Location: {location_label(job)}"),
        (f"<b>Employment Type:</b> {_e(employment_label(job))}", f"Employment Type: {employment_label(job)}"),
        (f"<b>Match Score:</b> {sc['score']:.1f}/10 – {_e(sc.get('label'))} (confidence: {_e(sc.get('confidence'))})",
         f"Match Score: {sc['score']:.1f}/10 – {sc.get('label')} (confidence: {sc.get('confidence')})"),
        (f"<b>Posted:</b> {_e(posted_label(job))}", f"Posted: {posted_label(job)}"),
        (f"<b>Source:</b> {_e(source_label(job))}{' · via recruitment agency' if job.get('is_agency') else ''}",
         f"Source: {source_label(job)}{' · via recruitment agency' if job.get('is_agency') else ''}"),
    ]
    if is_eu():
        vl = visa_line(sc.get("visa"))
        lines.append((f"<b>Visa:</b> {_e(vl)}", f"Visa: {vl}"))
    lines += [("", ""), ("<b>Why You Match:</b>", "Why You Match:")]
    lines += [(f"• {_e(r)}", f"- {r}") for r in why[:3]] or [("• (see description)", "- (see description)")]
    lines += [("", ""), ("<b>Potential Gaps:</b>", "Potential Gaps:")]
    lines += [(f"• {_e(g)}", f"- {g}") for g in gaps[:3]] or [("• None significant detected", "- None significant detected")]
    if unc:
        lines += [("", ""), ("<b>Check before applying:</b>", "Check before applying:")]
        lines += [(f"• {_e(u)}", f"- {u}") for u in unc[:3]]
    lines += [("", ""), (f"<b>Recommendation:</b> {_e(sc.get('recommendation'))}", f"Recommendation: {sc.get('recommendation')}"),
              ("", ""), ("<b>Application Link:</b>", "Application Link:"), (_e(job.get("url")), job.get("url") or "")]
    if job.get("id") and (not is_eu() or os.environ.get("JOBRADAR_TELEGRAM_COMMANDS") == "1"):
        lines += [("", ""), (f"Applied? Tap /applied_{job['id']}", f"Applied? Send /applied_{job['id']}")]
    elif job.get("id"):  # Europe alerts through the UAE bot: its commands would hit the UAE list → tick on the dashboard
        lines += [("", ""), ("Applied? Tick it on your Europe dashboard.", "Applied? Tick it on your Europe dashboard.")]
    return "\n".join(h for h, _ in lines), "\n".join(p for _, p in lines)


def digest(date_label: str, rows: list[dict], leads: list[dict], health: dict) -> tuple[str, str]:
    h = [f"<b>JobRadar daily digest – {_e(date_label)}</b>", ""]
    p = [f"JobRadar daily digest – {date_label}", ""]
    if rows:
        h.append(f"<b>{len(rows)} matching vacancies in the last 24h</b>")
        p.append(f"{len(rows)} matching vacancies in the last 24h")
        for r in rows[:20]:
            h.append(f"• <b>{r['score']:.1f}</b> {_e(r['title'])} – {_e(r['company_display'])}\n  {_e(r['url'])}")
            p.append(f"- {r['score']:.1f} {r['title']} – {r['company_display']}\n  {r['url']}")
    else:
        h.append("No new matching vacancies in the last 24h.")
        p.append("No new matching vacancies in the last 24h.")
    if leads:
        h += ["", f"<b>Unverified job-board leads ({len(leads)})</b>"]
        p += ["", f"Unverified job-board leads ({len(leads)})"]
        for r in leads[:10]:
            h.append(f"• {r['score']:.1f} {_e(r['title'])} – {_e(r['company_display'])}\n  {_e(r['url'])}")
            p.append(f"- {r['score']:.1f} {r['title']} – {r['company_display']}\n  {r['url']}")
    hl = (f"Sources OK {health['ok']}/{health['monitored']} · failing {health['failing']} · "
          f"jobs analysed {health['jobs_total']} · last scan {health['last_scan']}")
    h += ["", f"<i>{_e(hl)}</i>"]
    p += ["", hl]
    return "\n".join(h), "\n".join(p)


def baseline(company: str, rows: list[dict], total: int) -> tuple[str, str]:
    h = [f"<b>Initial scan – {_e(company)}</b>", f"{total} vacancies in {place_label()} indexed; top matches:"]
    p = [f"Initial scan – {company}", f"{total} vacancies in {place_label()} indexed; top matches:"]
    for r in rows:
        h.append(f"• <b>{r['score']:.1f}</b> {_e(r['title'])}\n  {_e(r['url'])}")
        p.append(f"- {r['score']:.1f} {r['title']}\n  {r['url']}")
    return "\n".join(h), "\n".join(p)

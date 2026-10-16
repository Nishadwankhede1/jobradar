"""Telegram commands, read at the start of every cloud scan (no server needed).

From the alert message or the dashboard you can send:
    /applied_123          mark job 123 as applied      (dashboard ✓ sends "/start ap123")
    /unapply_123          move it back                 ("/start un123")
    /stage_123_interview  applied | interview | offer | rejected | withdrawn   ("/start st123_interview")
    /applied              list your recent applications
    any job link          add a job JobRadar can't see itself (e.g. Indeed "Jobs for you"): share the job to the bot,
                          or paste the link with the title / company / description – it is scored, gets a tailored CV
                          and shows on the dashboard after the next scan. A description that Telegram splits into
                          a second message is joined automatically.
Only messages from TELEGRAM_CHAT_ID (your own chat with the bot) are accepted; anything else is ignored.
"""
from __future__ import annotations

import json
import os
import re

from ..applications import STATUSES, all_applications, set_applied
from ..util import log, now_iso, sha

CMD = re.compile(r"^/(?:start\s+)?(ap|applied|un|unapply|st|stage)[_\s-]?(\d+)(?:[_\s-]+([a-z]+))?\s*$", re.I)
HELP = ("JobRadar is connected.\n\nTap /applied_<id> under an alert (or ✓ on the dashboard) after you apply – the job "
        "moves to your Applied list and you won't be alerted about it again.\n/applied – your recent applications\n\n"
        "Found a job JobRadar missed? Share it to this chat, or paste the link + title + company + description. "
        "It is scored and added to your dashboard at the next scan.")

URL_RX = re.compile(r"https?://[^\s<>\"']+")
UAE_RX = re.compile(r"\b(dubai|abu dhabi|sharjah|ajman|ras al khaimah|fujairah|umm al quwain|al ain|united arab emirates|uae)\b", re.I)
NOISE_RX = re.compile(r"^(check out this job|i found this job|job (?:link|opportunity)|apply (?:now|here)|via indeed|"
                      r"shared (?:via|from)|easily apply|urgently hiring|new|full[- ]time|permanent|in person)\b[:!.]?\s*$", re.I)
PAY_RX = re.compile(r"\b(aed|usd|salary|per month|a month|/month)\b", re.I)
JOINER_GAP = 180  # seconds: a long description split by Telegram arrives as a 2nd message right after the link


def _job_ref(url: str) -> tuple[str, str]:
    """(source, external_id) – the same keys the job-alert e-mails use, so a later alert updates this job."""
    m = re.search(r"indeed\.[a-z.]+/.*?\b(?:jk|vjk)=([0-9a-f]{8,})", url)
    if m:
        return "email:indeed", m.group(1)
    m = re.search(r"linkedin\.com/.*?(?:jobs/view/(?:[^/?]*-)?|currentJobId=)(\d{6,})", url)
    if m:
        return "email:linkedin", m.group(1)
    return "telegram", sha(url.split("#")[0])[:16]


def parse_shared_job(text: str) -> dict | None:
    """Title / company / location / description from a shared or pasted job. None when there is no link."""
    m = URL_RX.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(").,;")
    body = re.sub(r"^/add\b", "", URL_RX.sub("\n", text.strip()), flags=re.I)
    lines = [ln.strip(" \t-–•|·:") for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not NOISE_RX.match(ln)]
    title = company = ""
    head = lines[0] if lines else ""
    # one-line shares: "Title - Company - Dubai", "Title at Company", "Company is hiring: Title"
    m1 = re.match(r"(?i)(.+?) is hiring(?: for| an?)?\s*[:\-–]?\s*(.+)$", head)
    m2 = re.match(r"(?i)(?:check out this job at )?(.+?),\s*(.+)$", head) if "check out" in text.lower() else None
    parts = [p.strip() for p in re.split(r"\s+[-–|•·]\s+", head) if p.strip()]
    if m1:
        company, title = m1.group(1), m1.group(2)
    elif m2:
        company, title = m2.group(1), m2.group(2)
    elif len(parts) >= 2:
        title, company = parts[0], parts[1]
    elif re.search(r"\s+at\s+", head) and len(head) < 140:
        title, company = re.split(r"\s+at\s+", head, maxsplit=1)
    else:
        title = head
        if len(lines) > 1 and len(lines[1]) < 90 and not PAY_RX.search(lines[1]):
            company = UAE_RX.sub("", lines[1]).strip(" ,-–•·|")
    rest = lines[1:] if not company or company not in (lines[1] if len(lines) > 1 else "") else lines[2:]
    desc = "\n".join(rest).strip()
    loc = UAE_RX.search(text)
    return {"url": url, "title": title[:140].strip(), "company": UAE_RX.sub("", company).strip(" ,-–•·|")[:100] or "Unknown company",
            "location": loc.group(0).title() if loc else "United Arab Emirates",
            "description": desc if len(desc) >= 120 else None}


def add_shared_job(db, text: str, when: int | None = None) -> str | None:
    from ..models import RawJob
    from ..pipeline import dedup, upsert_job
    v = parse_shared_job(text)
    if v is None:
        return None
    if len(v["title"]) < 4:
        return ("Got the link, but I can't read the job from it (job sites don't allow that). Send it again with the "
                "title, company and the description pasted under the link.")
    if not db.one("SELECT 1 FROM companies WHERE id='email-alerts'"):
        db.upsert("companies", {"id": "email-alerts", "name": "Job-alert e-mails", "kind": "aggregator", "ats": "email",
                                "enabled": 0, "created_at": now_iso(), "updated_at": now_iso()}, ["id"])
    agg = {"id": "email-alerts", "name": "Job-alert e-mails", "kind": "aggregator", "industry": "", "priority": 3}
    source, ext = _job_ref(v["url"])
    rj = RawJob(source=source, external_id=ext, title=v["title"], url=v["url"], location=v["location"],
                description=v["description"], company_name=v["company"], is_official=False,
                extra={"via": "shared by you on Telegram"})
    jid, change = upsert_job(db, agg, rj, now_iso(), baseline=False)
    db.update("jobs", {"id": jid}, {"verification": "unverified", "evidence": f"shared by you on Telegram {now_iso()[:16]}"})
    dedup(db, [jid])
    db.kv_set("telegram_last_add", {"job": jid, "at": when or 0})
    db.commit()
    what = "with the description" if v["description"] else "title only – paste the description in a reply for a proper score"
    return (f"➕ {'Added' if change == 'new' else 'Updated'}: {v['title']} – {v['company']} ({what}).\n"
            "It is scored, gets a tailored CV and shows on your dashboard after the next scan (≈15 min).")


def append_description(db, text: str, when: int | None) -> str | None:
    """The 2nd half of a long paste (Telegram splits messages over 4096 characters)."""
    last = db.kv_get("telegram_last_add", None) or {}
    if not last.get("job") or not when or when - int(last.get("at") or 0) > JOINER_GAP or len(text) < 120:
        return None
    job = db.one("SELECT id, title, description FROM jobs WHERE id=?", (last["job"],))
    if not job:
        return None
    desc = ((job["description"] or "") + "\n" + text.strip()).strip()
    db.update("jobs", {"id": job["id"]}, {"description": desc, "content_hash": sha(job["title"], desc[:20000])})
    db.kv_set("telegram_last_add", {"job": job["id"], "at": when})
    db.commit()
    return f"📝 Added that text to the description of: {job['title']}"


def _reply(http, token, chat, text):
    try:
        http.post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       json_body={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
    except Exception as e:  # a failed confirmation must not stop the scan
        log.warning(f"telegram reply failed: {type(e).__name__}")


def handle_text(db, text: str, when: int | None = None) -> str | None:
    """Apply one command; returns the confirmation text (None = not a JobRadar command)."""
    text = (text or "").strip()
    if not text.startswith("/") or text.lower().startswith("/add"):
        if URL_RX.search(text):
            return add_shared_job(db, text, when)
        return append_description(db, text, when)
    if text.lower() in ("/start", "/help"):
        return HELP
    if text.lower() in ("/applied", "/list"):
        apps = all_applications(db)[:15]
        if not apps:
            return "No applications yet. Tap /applied_<id> under an alert after you apply."
        return "Your applications:\n" + "\n".join(f"• {a['title']} – {a['company']} ({a['status']}, {str(a['applied_at'])[:10]})"
                                                for a in apps)
    m = CMD.match(text)
    if not m:
        return None
    verb, jid, arg = m.group(1).lower(), int(m.group(2)), (m.group(3) or "").lower()
    job = db.one("SELECT title, company_display FROM jobs WHERE id=?", (jid,))
    if not job:
        return f"Job {jid} isn't in JobRadar any more."
    label = f"{job['title']} – {job['company_display']}"
    if verb in ("un", "unapply"):
        set_applied(db, jid, False)
        return f"↩ Moved back to your job lists: {label}"
    if verb in ("st", "stage"):
        if arg not in STATUSES:
            return "Stage must be one of: " + ", ".join(STATUSES)
        set_applied(db, jid, True, arg)
        return f"Stage set to {arg}: {label}"
    set_applied(db, jid, True)
    return f"✅ Marked as applied: {label}\nYou won't get alerts for this role again."


def process_commands(db, http) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return 0
    offset = int(db.kv_get("telegram_offset", 0) or 0)
    try:
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset + 1
        updates = http.get_json(f"https://api.telegram.org/bot{token}/getUpdates", params=params).get("result") or []
    except Exception as e:
        log.warning(f"telegram commands skipped: {type(e).__name__}")
        return 0
    n = 0
    for u in updates:
        offset = max(offset, int(u.get("update_id") or 0))
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue  # only you can change your data
        try:
            reply = handle_text(db, msg.get("text") or msg.get("caption") or "", msg.get("date"))
        except Exception as e:  # a bad message must not stop the scan
            log.warning(f"telegram command failed: {type(e).__name__}")
            reply = "Sorry, I couldn't add that one – try pasting the title, company and description under the link."
        if reply:
            n += 1
            _reply(http, token, chat, reply)
    db.kv_set("telegram_offset", offset)
    db.commit()
    return n


def bot_username(http) -> str | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        return (http.get_json(f"https://api.telegram.org/bot{token}/getMe").get("result") or {}).get("username")
    except Exception:
        return None

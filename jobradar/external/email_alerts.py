"""Compliant coverage for LinkedIn, Indeed, Bayt, GulfTalent, Naukrigulf, Glassdoor, Foundit:
set up job alerts on those sites (to a dedicated Gmail/Outlook address) and JobRadar reads the alert e-mails over
IMAP with an app password. No scraping of those platforms happens.

Env: IMAP_HOST (default imap.gmail.com), IMAP_USER, IMAP_PASSWORD (app password), IMAP_FOLDER (default INBOX).
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import timedelta
from email.header import decode_header, make_header
from html.parser import HTMLParser

from ..models import RawJob
from ..util import log, norm_space, utcnow

PLATFORMS = {
    "linkedin": (re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)"), "https://www.linkedin.com/jobs/view/{id}/"),
    "indeed": (re.compile(r"indeed\.com/[^\s\"']*?\bjk=([0-9a-f]{8,})"), "https://ae.indeed.com/viewjob?jk={id}"),
    "bayt": (re.compile(r"bayt\.com/en/[a-z-]+/jobs/[^\s\"'?]*?-(\d{5,})/?"), None),
    "gulftalent": (re.compile(r"gulftalent\.com/[^\s\"']*?jobs/[^\s\"'?]*?-(\d{4,})"), None),
    "naukrigulf": (re.compile(r"naukrigulf\.com/[^\s\"'?]*?-jid-(\d+)"), None),
    "glassdoor": (re.compile(r"glassdoor\.[a-z.]+/[^\s\"']*?(?:jobListingId|jl)=(\d+)"), None),
    "foundit": (re.compile(r"foundit\.(?:ae|in|com)/[^\s\"']*?job/[^\s\"'?]*?-(\d{5,})"), None),
}
SENDER_HINTS = ("linkedin", "indeed", "bayt", "gulftalent", "naukrigulf", "glassdoor", "foundit", "monster")


def load_employer_alerts(path=None) -> list[dict]:
    """config/employer_alerts.json: employers whose own careers system e-mails new vacancies to the user."""
    import json
    from pathlib import Path
    p = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "employer_alerts.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def employer_for_sender(sender: str, entries: list[dict]) -> dict | None:
    s = (sender or "").lower()
    for e in entries:
        if any(h.lower() in s for h in e.get("senders", [])):
            return e
    return None


def parse_employer_alert(html_body: str, entry: dict, sender: str = "") -> list[RawJob]:
    """Job links in an alert e-mail sent by the employer's own careers system. These are official postings
    (is_official=True); only the title/location shown in the e-mail are known until the posting is opened."""
    p = _Anchors()
    try:
        p.feed(html_body or "")
    except Exception:
        return []
    rx = re.compile(entry["link_pattern"], re.I)
    out: dict[str, RawJob] = {}
    for it in p.items:
        href, text = it["href"], norm_space(it["text"])
        m = rx.search(href or "")
        if not m:
            continue
        url = "https://" + m.group(0).split("://")[-1] if "://" not in m.group(0) else m.group(0)
        url = re.sub(r"[?&](utm_[^=&]+|trk|src|source|ref)=[^&]*", "", url)
        if not text or BAD_ANCHOR.search(text) or len(text) < 4:
            continue
        idm = re.search(r"(\d{4,})", url)
        jid = idm.group(1) if idm else url
        after = [a for a in it["after"] if a and not BAD_ANCHOR.search(a)]
        location = next((a for a in after if re.search(r"dubai|uae|emirates|abu dhabi|sharjah|remote", a, re.I)), "")
        out.setdefault(jid, RawJob(source="email:employer", external_id=jid, title=text, url=url, location=location,
                                   description=None, company_name=entry["name"], is_official=True,
                                   extra={"via": "employer alert e-mail", "sender": sender,
                                          "company_id": entry.get("company_id")}))
    return list(out.values())
BAD_ANCHOR = re.compile(r"^(view|apply|see|show|more|unsubscribe|manage|search|jobs? alert|easy apply|save)\b", re.I)


class _Anchors(HTMLParser):
    """Collect (href, anchor_text, following_texts) triples from an HTML e-mail."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self._cur, self._skip = [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "a":
            self._cur = {"href": dict(attrs).get("href", ""), "text": "", "after": []}
            self.items.append(self._cur)

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip:
            self._skip -= 1
        if tag == "a" and self._cur is not None:
            self._cur["closed"] = True

    def handle_data(self, data):
        if self._skip or not data.strip():
            return
        if self._cur is not None and not self._cur.get("closed"):
            self._cur["text"] += data
        elif self.items:
            last = self.items[-1]
            if len(last["after"]) < 4:
                last["after"].append(norm_space(data))


def parse_alert_email(html_body: str, sender: str = "") -> list[RawJob]:
    p = _Anchors()
    try:
        p.feed(html_body or "")
    except Exception:
        return []
    out: dict[str, RawJob] = {}
    for it in p.items:
        href, text = it["href"], norm_space(it["text"])
        for name, (rx, canon) in PLATFORMS.items():
            m = rx.search(href)
            if not m:
                continue
            if not text or BAD_ANCHOR.search(text) or len(text) < 4:
                if m.group(1) in out:
                    break
                continue
            after = [a for a in it["after"] if a and not BAD_ANCHOR.search(a)]
            company = after[0] if after else None
            location = next((a for a in after[1:] if re.search(r"dubai|uae|emirates|abu dhabi|sharjah|remote", a, re.I)),
                            after[1] if len(after) > 1 else "")
            url = canon.format(id=m.group(1)) if canon else href.split("?")[0]
            out[m.group(1)] = RawJob(source=f"email:{name}", external_id=m.group(1), title=text, url=url,
                                     location=location or "", description=None, company_name=company,
                                     is_official=False, extra={"via": f"{name} alert e-mail", "sender": sender})
            break
    return list(out.values())


def fetch_email_leads(db, days: int = 3, max_messages: int = 200) -> tuple[list[RawJob], str]:
    host, user, pwd = os.environ.get("IMAP_HOST", "imap.gmail.com"), os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not (user and pwd):
        return [], "IMAP_USER/IMAP_PASSWORD not set – e-mail alert ingestion disabled"
    leads: list[RawJob] = []
    employers = load_employer_alerts()
    since = (utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL(host)
        M.login(user, pwd)
        M.select(os.environ.get("IMAP_FOLDER", "INBOX"), readonly=True)
        typ, data = M.search(None, f'(SINCE "{since}")')
        ids = (data[0] or b"").split()[-max_messages:]
        for mid in ids:
            typ, msg_data = M.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            message_id = msg.get("Message-ID") or f"{mid.decode()}-{msg.get('Date')}"
            sender = str(make_header(decode_header(msg.get("From", "")))).lower()
            emp = employer_for_sender(sender, employers)
            if (not emp and not any(h in sender for h in SENDER_HINTS)) or db.email_seen(message_id):
                continue
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    break
            leads += parse_employer_alert(body, emp, sender) if emp else parse_alert_email(body, sender)
            db.mark_email_seen(message_id)
        M.logout()
    except Exception as e:  # never log credentials – only the error type
        log.warning("email alert ingestion failed: %s", type(e).__name__)
        return leads, f"IMAP error: {type(e).__name__}"
    return leads, f"{len(leads)} leads from alert e-mails"

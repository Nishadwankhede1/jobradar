"""Replies to jobs you applied to – interview invites, assessments, offers, rejections, "we received your application" –
spotted in the same mailbox JobRadar already reads for job alerts (IMAP, read-only, app password).

Nothing changes on its own: every match is a *suggestion* ("📩 Looks like an interview invite") that you confirm or dismiss
in the app or on the dashboard (an encrypted {"ms": id, "ok": true|false} tick). Interview invites, assessments and offers
are alerted at once. Only a matched e-mail's subject, sender, date and a short snippet are kept, in the encrypted database;
e-mails that match none of your applications are skipped and only a hash of their Message-ID is remembered.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import os
import re
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape

from ..util import iso, jaccard, log, norm_space, normalize_company, normalize_title, now_iso, parse_dt, utcnow

KV_EVENTS, KV_SEEN = "mail_status", "mail_status_seen"
# most decisive first: a rejection that thanks you for applying is a rejection; an assessment invite that mentions the
# "interview process" is an assessment
KINDS = [
    ("offer", re.compile(r"\b(offer letter|pleased to (make you an |extend (you )?an )?offer|job offer|offer of employment|"
                         r"we(?:'d| would) like to offer you|contract of employment)\b", re.I)),
    ("rejected", re.compile(r"\bunfortunately\b|\bnot (?:to )?(?:be )?(?:moving|proceed|progress)|decided to (?:move|proceed|go) "
                            r"(?:forward )?with (?:other|another)|other candidates|not been successful|\bunsuccessful\b|"
                            r"regret to inform|no longer (?:under )?consider|position has (?:now )?been filled|"
                            r"will not be (?:taking|moving) your application", re.I)),
    ("assessment", re.compile(r"\bassessment\b|online test|coding (?:test|challenge)|hackerrank|codility|testgorilla|hirevue|"
                              r"\bshl\b|psychometric|aptitude test|take-?home (?:task|test|assignment)|case study", re.I)),
    ("interview", re.compile(r"\binterview(?:s|ing)?\b|schedule (?:a|an|your) (?:call|chat|conversation|meeting)|"
                             r"book (?:a|your) (?:time|slot)|your availability|calendly\.com|phone screen|screening call|"
                             r"meet (?:with )?the (?:team|hiring manager)", re.I)),
    ("received", re.compile(r"thank(?:s| you) for (?:applying|your application|your interest)|application (?:has been |was )?"
                            r"(?:received|submitted|sent)|we(?:'ve| have) received your application", re.I)),
]
ALERT_KINDS = ("interview", "assessment", "offer")   # worth a ping now
STAGE = {"interview": "interview", "assessment": "interview", "offer": "offer", "rejected": "rejected"}
# job-alert digests are read by email_alerts.py; they are never about an application you made
SKIP_SENDERS = re.compile(r"jobalerts-noreply@linkedin|jobs-listings@linkedin|alert@indeed|noreply@glassdoor|"
                          r"@bayt\.com|@gulftalent|@naukrigulf|@foundit", re.I)


class _Text:
    TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)

    @classmethod
    def of(cls, msg) -> str:
        plain, html = "", ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct not in ("text/plain", "text/html") or part.get_filename():
                continue
            try:
                s = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ct == "text/plain" and not plain:
                plain = s
            elif ct == "text/html" and not html:
                html = s
        return norm_space(plain or unescape(cls.TAG.sub(" ", html)))


def classify(subject: str, body: str) -> str | None:
    text = f"{subject}\n{body[:4000]}"
    for kind, rx in KINDS:
        if rx.search(text):
            return kind
    return None


# words that don't identify an employer on their own ("Dubai Holding" must not match every e-mail that says "Dubai")
GENERIC = {"the", "and", "for", "dubai", "abu", "dhabi", "sharjah", "emirates", "arab", "united", "gulf", "arabia", "middle",
           "east", "international", "global", "national", "services", "solutions", "technologies", "technology", "systems",
           "consulting", "partners", "properties", "investments", "trading", "industries", "careers", "talent", "team"}


def _words(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()) + " "


def _employer_in(name: str, hay: str) -> bool:
    words = normalize_company(name).split()
    distinct = [w for w in words if len(w) >= 3 and w not in GENERIC]
    if distinct:
        return all(f" {w} " in hay for w in distinct[:2])
    # only generic words left ("Dubai Holding") → the whole name as written, minus legal suffixes, as a phrase
    phrase = norm_space(re.sub(r" (llc|l l c|ltd|limited|inc|plc|pjsc|psc|fze|fzco|dmcc|co) ", " ", _words(name)))
    return len(phrase.split()) >= 2 and f" {phrase} " in hay


def match_application(apps: list[dict], sender: str, subject: str, body: str) -> dict | None:
    """The application an e-mail is about: the employer's name must appear (sender, subject or text); among several roles
    at that employer, the one whose title fits best. None when it's about none of them."""
    hay = _words(f"{sender} {subject} {body[:4000]}")
    best, best_score = None, 0.0
    for a in apps:
        if not _employer_in(a.get("company") or "", hay):
            continue
        t = jaccard(a.get("norm_title") or normalize_title(a.get("title") or ""), normalize_title(f"{subject} {body[:600]}"))
        score = 1.0 + t + (0.5 if _words(a.get("title") or "").strip() and _words(a.get("title") or "") in hay else 0.0)
        if score > best_score:
            best, best_score = a, score
    return best


def _hdr(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def scan_status_mail(db, days: int = 21, max_messages: int = 300) -> list[dict]:
    """New suggestions found in the mailbox (also stored in kv `mail_status`). Never raises."""
    host, user, pwd = os.environ.get("IMAP_HOST") or "imap.gmail.com", os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    from ..applications import all_applications
    apps = [a for a in all_applications(db) if (a.get("status") or "applied") not in ("withdrawn",)]
    if not (user and pwd and apps):
        return []
    seen = set(db.kv_get(KV_SEEN, []) or [])
    events = db.kv_get(KV_EVENTS, []) or []
    known = {e["id"] for e in events}
    new: list[dict] = []
    since = (utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL(host)
        M.login(user, pwd)
        M.select(os.environ.get("IMAP_FOLDER", "INBOX"), readonly=True)   # read-only: nothing is marked as read
        _, data = M.search(None, f'(SINCE "{since}")')
        ids = (data[0] or b"").split()[-max_messages:]
        heads = []                                         # all headers in one round trip, bodies only for candidates
        if ids:
            _, hd = M.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            for part in hd:
                if isinstance(part, tuple) and part[0]:
                    m = re.match(rb"(\d+)", part[0])
                    if m:
                        heads.append((m.group(1), email.message_from_bytes(part[1])))
        for mid, head in heads:
            key = hashlib.sha256((head.get("Message-ID") or f"{mid.decode()}|{head.get('Date')}").encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            sender = _hdr(head.get("From"))
            if SKIP_SENDERS.search(sender) or user.lower() in sender.lower():
                continue
            _, md = M.fetch(mid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(md[0][1])
            subject, body = _hdr(msg.get("Subject")), _Text.of(msg)
            kind = classify(subject, body)
            if not kind:
                continue
            a = match_application(apps, sender, subject, body)
            if not a:
                continue
            try:
                when = iso(parsedate_to_datetime(head.get("Date")))
            except Exception:
                when = now_iso()
            applied = parse_dt(a.get("applied_at"))
            if applied and when and parse_dt(when) and parse_dt(when) < applied - timedelta(days=1):
                continue                                   # older than your application → not a reply to it
            if key in known:
                continue
            name, addr = parseaddr(sender)
            ev = {"id": key, "job_id": a["job_id"], "company": a.get("company"), "title": a.get("title"), "kind": kind,
                  "subject": subject[:200], "sender": (name or addr.split("@")[0])[:80] + (f" ({addr.split('@')[-1]})" if "@" in addr else ""),
                  "date": when, "snippet": body[:220], "state": "new", "found_at": now_iso()}
            events.append(ev)
            new.append(ev)
        M.logout()
    except Exception as e:                                 # never log credentials – only the error type
        log.warning("status e-mail check failed: %s", type(e).__name__)
    db.kv_set(KV_EVENTS, events[-300:])
    db.kv_set(KV_SEEN, sorted(seen)[-8000:])
    db.commit()
    return new


def resolve(db, event_id: str, ok: bool) -> bool:
    """Your answer to a suggestion: confirm → the application moves to that stage; dismiss → it's dropped."""
    from ..applications import set_applied
    events = db.kv_get(KV_EVENTS, []) or []
    for e in events:
        if e["id"] != event_id or e.get("state") != "new":
            continue
        e["state"], e["resolved_at"] = ("confirmed" if ok else "dismissed"), now_iso()
        if ok and STAGE.get(e["kind"]):
            try:
                set_applied(db, int(e["job_id"]), True, STAGE[e["kind"]])
            except KeyError:
                pass
        db.kv_set(KV_EVENTS, events)
        db.commit()
        return True
    return False


def recent(db, days: int = 60) -> list[dict]:
    """Suggestions for the dashboard / app (newest first)."""
    cut = iso(utcnow() - timedelta(days=days))
    return sorted((e for e in (db.kv_get(KV_EVENTS, []) or []) if (e.get("date") or "") >= cut),
                  key=lambda e: e.get("date") or "", reverse=True)

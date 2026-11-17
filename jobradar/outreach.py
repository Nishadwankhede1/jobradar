"""People you reached out to on LinkedIn (hiring managers, team leads, recruiters) – and why.

Each person is saved from the app (an encrypted {"ox": id, …} tick): name, title, company, profile link, the role you
contacted them about, the note you sent and when. JobRadar then watches the same mailbox it reads for job alerts for
LinkedIn's "<Name> accepted your invitation" and "<Name> sent you a message" e-mails, so that weeks later the app can
tell you "Ahmed accepted – you wrote to him about the BI Analyst role on 12 Nov" with a follow-up ready.

Nothing is ever sent to anyone: JobRadar only reads e-mail subjects (read-only) and keeps the names and dates of those
LinkedIn notices, in the encrypted database, for 90 days.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from .util import iso, now_iso, parse_dt, utcnow

KV_PEOPLE, KV_EVENTS = "outreach", "linkedin_events"
STATES = ("sent", "followed_up", "closed")

# LinkedIn notice subjects → (kind, name). Most specific first.
SUBJECTS = [
    ("accepted", re.compile(r"^(?P<n>.+?) (?:has )?accepted your invitation", re.I)),
    ("accepted", re.compile(r"^you and (?P<n>.+?) are now connected", re.I)),
    ("accepted", re.compile(r"start a conversation with your new connection,? (?P<n>.+?)\s*$", re.I)),
    ("message", re.compile(r"^(?:new message from|you have a new message from|message from) (?P<n>.+?)\s*$", re.I)),
    ("message", re.compile(r"^(?P<n>.+?) (?:sent you a (?:new )?message|just messaged you|messaged you|replied to (?:you|your message)|"
                           r"has sent you a message)", re.I)),
    ("message", re.compile(r"^(?:re:\s*)?(?P<n>[^:,]{3,60}?) (?:replied|responded)\b", re.I)),
]
LINKEDIN_SENDER = re.compile(r"linkedin", re.I)
_JOB_ALERTS = re.compile(r"jobalerts-noreply|jobs-listings|jobs-noreply", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(dr|mr|mrs|ms|eng|engr)\.?\b", " ", s.lower())
    return " ".join(re.sub(r"[^a-z ]+", " ", s).split())


def same_person(a: str, b: str, loose: bool = False) -> bool:
    """'Ahmed Al Mansoori' ↔ 'Ahmed Al-Mansoori, CFA' ↔ 'Ahmed Mansoori': first name equal and the last name of one in the other."""
    x, y = _norm(re.split(r"[,|(]", a or "")[0]).split(), _norm(re.split(r"[,|(]", b or "")[0]).split()
    if not x or not y or x[0] != y[0]:
        return False
    if len(x) == 1 or len(y) == 1:   # "Ahmed sent you a message": first name only counts for messages
        return len(x) == len(y) or loose
    return x[-1] in y or y[-1] in x


def parse_notice(sender: str, subject: str) -> tuple[str, str] | None:
    """(kind, name) for a LinkedIn connection / message e-mail, else None."""
    if not LINKEDIN_SENDER.search(sender or "") or _JOB_ALERTS.search(sender or ""):
        return None
    subj = re.sub(r"\s+", " ", subject or "").strip()
    for kind, rx in SUBJECTS:
        m = rx.search(subj)
        if m:
            name = m.group("n").strip(" .,!:-")
            if 2 <= len(name) <= 80 and not re.search(r"\b(linkedin|jobs?|people|connections?)\b", name, re.I):
                return kind, name
    return None


def note_event(db, kind: str, name: str, when: str | None) -> None:
    events = db.kv_get(KV_EVENTS, []) or []
    when = when or now_iso()
    if not any(e["kind"] == kind and e["name"] == name and e["date"][:10] == when[:10] for e in events):
        events.append({"kind": kind, "name": name, "date": when})
    cut = iso(utcnow() - timedelta(days=90))
    db.kv_set(KV_EVENTS, [e for e in events if e["date"] >= cut][-400:])


def save(db, it: dict) -> bool:
    """An {"ox": id, n, ti, c, p, j, ro, nt, at, st} tick: add or update one person (only the fields sent); "del" removes."""
    pid = str(it.get("ox") or "").strip()[:40]
    if not pid:
        return False
    people = db.kv_get(KV_PEOPLE, []) or []
    if it.get("del"):
        db.kv_set(KV_PEOPLE, [p for p in people if p["id"] != pid])
        return True
    rec = next((p for p in people if p["id"] == pid), None)
    if rec is None:
        if not str(it.get("n") or "").strip():
            return False
        rec = {"id": pid, "state": "sent", "created_at": now_iso()}
        people.append(rec)
    for k, field, n in (("n", "name", 80), ("ti", "title", 120), ("c", "company", 100), ("p", "profile", 300),
                        ("ro", "role", 140), ("nt", "note", 1200), ("fu", "follow_up", 1200), ("at", "sent_at", 30)):
        if it.get(k) is not None:
            rec[field] = str(it[k]).strip()[:n]
    if it.get("j") is not None:
        try:
            rec["job_id"] = int(it["j"]) or None
        except (TypeError, ValueError):
            rec["job_id"] = None
    if it.get("st") in STATES:
        rec["state"] = it["st"]
    rec.setdefault("sent_at", now_iso())
    rec["updated_at"] = now_iso()
    db.kv_set(KV_PEOPLE, people[-500:])
    return True


def listing(db) -> list[dict]:
    """Everyone you reached out to, newest first, with accepted_at / replied_at found in the LinkedIn e-mails."""
    events = db.kv_get(KV_EVENTS, []) or []
    jobs = {}
    out = []
    for p in db.kv_get(KV_PEOPLE, []) or []:
        p = dict(p)
        since = parse_dt(p.get("sent_at"))
        since = iso(since - timedelta(days=1)) if since else ""
        for e in sorted(events, key=lambda e: e["date"]):
            if e["date"] < since or not same_person(p.get("name") or "", e["name"], loose=e["kind"] == "message"):
                continue
            k = "accepted_at" if e["kind"] == "accepted" else "replied_at"
            p.setdefault(k, e["date"])
        if p.get("job_id") and p["job_id"] not in jobs:
            jobs[p["job_id"]] = db.one("SELECT title, company_display, url FROM jobs WHERE id=?", (p["job_id"],))
        j = jobs.get(p.get("job_id"))
        if j:
            p.setdefault("role", j["title"])
            p["job_url"] = j["url"]
            if not p.get("company"):
                p["company"] = j["company_display"]
        out.append(p)
    return sorted(out, key=lambda p: p.get("sent_at") or "", reverse=True)

"""Skills you confirm from the dashboard ("I know this") – added to every tailored CV whose job ad asks for them.

Kept in the database (kv 'user_skills'), never in the public code. Clicking a keyword under "Not in your CV" adds it;
clicking it again under "Added" removes it. Only add skills you can talk about in an interview.
"""
from __future__ import annotations

import re

from ..scoring.skills import COMPILED, HUMAN

KEY = "user_skills"


def resolve_sid(term: str) -> str | None:
    """The lexicon id whose pattern matches (almost) the whole keyword, e.g. 'Airflow' → 'airflow', 'GCP' → 'gcp'."""
    t = (term or "").strip()
    if not t:
        return None
    for sid, rx in COMPILED.items():
        m = rx.search(t)
        if m and len(m.group(0).strip()) >= 0.8 * len(t):
            return sid
    low = t.lower()
    return next((sid for sid, h in HUMAN.items() if h and h.lower() == low), None)


def get_user_skills(db) -> list[dict]:
    return [x for x in (db.kv_get(KEY, []) or []) if isinstance(x, dict) and x.get("term")]


def user_skill_keys(db) -> set[str]:
    """Lower-case terms + lexicon ids, the form tailor() checks against."""
    keys = set()
    for x in get_user_skills(db):
        keys.add(x["term"].lower())
        if x.get("sid"):
            keys.add(x["sid"])
    return keys


def set_user_skill(db, term: str, on: bool = True) -> bool:
    """Add or remove one skill. Returns True when something changed."""
    term = re.sub(r"\s+", " ", str(term or "")).strip()[:60]
    if not term:
        return False
    cur = get_user_skills(db)
    sid = resolve_sid(term)
    same = lambda x: x["term"].lower() == term.lower() or (sid and x.get("sid") == sid)  # noqa: E731
    exists = any(same(x) for x in cur)
    if on and not exists:
        cur.append({"term": term, "sid": sid})
    elif not on and exists:
        cur = [x for x in cur if not same(x)]
    else:
        return False
    db.kv_set(KEY, cur[-200:])
    db.commit()
    return True

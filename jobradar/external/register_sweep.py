"""Netherlands: make sure no visa-sponsoring employer is missed.

A Dutch work visa needs an employer on the IND register of recognised sponsors, so that register *is* the complete
list of employers that matter. Each Europe scan takes the next batch of register names and probes the documented
public job-board feeds (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee) under the employer's full name.
An employer that has a feed with open jobs is followed from then on like any other employer. One full pass takes about a
week; then it starts again, so employers that start hiring (or switch job board) are picked up on the next pass.
Only full-name slugs are tried, and a Greenhouse board must carry the same full name, so another company's board is never
attributed to a register employer.
"""
from __future__ import annotations

import json
import logging
import re

from ..adapters.auto import _probes, has_jobs as _has_jobs
from ..http import HttpError, NotModified
from ..scoring.sponsor import REGISTER_FILE, norm_org
from ..util import now_iso, slugify

log = logging.getLogger("jobradar")
SKIP = re.compile(r"\b(universiteit|hogeschool|ziekenhuis|gemeente|umc|basisschool)\b", re.I)
KV = "register_sweep:{}"


def register_names(country: str) -> list[str]:
    p = REGISTER_FILE.get(country)
    if not p or not p.exists():
        return []
    try:
        return sorted(set(json.loads(p.read_text(encoding="utf-8")).get("names") or []), key=str.lower)
    except (OSError, ValueError):
        return []


def slugs_for(name: str) -> list[str]:
    words = norm_org(name).split()
    if not words:
        return []
    out = ["".join(words)] + (["-".join(words)] if len(words) > 1 else [])
    return [s for s in out if len(s) >= 4]


def probe(http, name: str) -> tuple[str, dict] | None:
    for slug in slugs_for(name):
        for ats, conf, url, ok in _probes(slug, recruitee=True):
            try:
                data = http.get_json(url)
                if not (ok(data) and _has_jobs(ats, data)):
                    continue
            except HttpError as e:
                if e.status == 429 or (e.status == 0 and "budget" in str(e)):
                    raise   # rate-limited / out of time → resume with this employer next scan
                continue
            except (ValueError, NotModified, AttributeError, TypeError):
                continue
            if ats == "greenhouse":  # a board under this slug must belong to this employer
                try:
                    meta = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                except (HttpError, ValueError, NotModified):
                    continue
                if "".join(norm_org(meta.get("name") or "").split()) != "".join(norm_org(name).split()):
                    continue   # same full name only – "Elastic" is not "Elastic Media Group"
            return ats, conf
    return None


def sweep(db, http, country: str = "nl", limit: int = 20, interval: int = 60) -> list[str]:
    """Probe the next `limit` register employers we don't follow yet; follow the ones with a live feed."""
    if limit <= 0:
        return []
    names = register_names(country)
    if not names:
        return []
    st = db.kv_get(KV.format(country), {}) or {}
    i, cycle = int(st.get("i") or 0), int(st.get("cycle") or 1)
    known = {norm_org(r["name"]) for r in db.q("SELECT name FROM companies")}  # "Adyen" == "Adyen N.V."
    added, probed = [], 0
    try:
        while probed < limit:
            if i >= len(names):
                i, cycle = 0, cycle + 1
                if probed == 0 and not added:
                    break
            name = names[i]
            i += 1
            if SKIP.search(name) or norm_org(name) in known or not slugs_for(name):
                continue
            probed += 1
            hit = probe(http, name)
            if not hit:
                continue
            cid = f"{country}reg-" + slugify(name)
            now = now_iso()
            db.upsert("companies", {"id": cid, "name": name, "kind": "lead", "ats": hit[0], "ats_config": hit[1],
                                    "priority": 2, "enabled": 1, "verified": 1, "created_at": now, "updated_at": now,
                                    "verification_note": f"On the official sponsor register; its {hit[0]} job feed was found "
                                                         f"automatically (register sweep)"}, ["id"])
            db.upsert("sources", {"company_id": cid, "interval_minutes": interval, "next_due_at": None,
                                  "last_status": None}, ["company_id"])
            known.add(norm_org(name))
            added.append(name)
    except HttpError:  # rate-limited or run-time budget used up → carry on from this employer next scan
        i = max(0, i - 1)
    db.kv_set(KV.format(country), {"i": i, "cycle": cycle, "total": len(names), "updated": now_iso(),
                                   "found": int(st.get("found") or 0) + len(added)})
    db.commit()
    if added:
        log.info("register sweep: now following %d sponsor(s): %s", len(added), ", ".join(added))
    return added

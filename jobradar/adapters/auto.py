"""'auto' – find which public ATS job-board feed an employer uses, remember it, then read it like a normal source.

Company entry: {"name": "Adyen", "ats": "auto", "ats_config": {"slugs": ["adyen"]}}   (slugs optional – derived from
the name). Probes the documented public feeds of Greenhouse, Lever, Ashby, SmartRecruiters, Workable and Recruitee
(EU hosts too). The hit is cached in the source's state; a miss is re-probed after a week. No scraping of HTML pages.
"""
from __future__ import annotations

import re
import time

from ..http import HttpError, NotModified
from .base import Adapter, AdapterResult, FetchContext, get_adapter, register

REPROBE_SECONDS = 7 * 86400


def slug_candidates(name: str, extra: list[str] | None = None) -> list[str]:
    base = re.sub(r"\(.*?\)", " ", (name or "").lower())
    base = re.sub(r"\b(b\.?v\.?|n\.?v\.?|gmbh|ag|se|ltd|limited|plc|inc|llc|group|holding|holdings|the|netherlands|nederland|"
                  r"europe|international|technologies|technology|labs)\b", " ", base)
    words = re.findall(r"[a-z0-9]+", base.replace(".com", "com").replace("&", " and "))
    out = list(extra or [])
    if words:
        out += ["".join(words), "-".join(words), words[0]]
        if base.strip().endswith("com") or ".com" in (name or "").lower():
            out.append("".join(words).replace("com", ""))
    return list(dict.fromkeys(s for s in out if s and len(s) >= 2))[:4]


def _probes(slug: str, recruitee: bool = False):
    """(ats, ats_config, probe url, test(json) -> bool) for one slug."""
    yield "greenhouse", {"board": slug}, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", lambda d: isinstance(d.get("jobs"), list)
    yield "greenhouse", {"board": slug, "region": "eu"}, f"https://boards-api.eu.greenhouse.io/v1/boards/{slug}/jobs", lambda d: isinstance(d.get("jobs"), list)
    yield "lever", {"site": slug}, f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", lambda d: isinstance(d, list)
    yield "lever", {"site": slug, "region": "eu"}, f"https://api.eu.lever.co/v0/postings/{slug}?mode=json&limit=1", lambda d: isinstance(d, list)
    yield "ashby", {"board": slug}, f"https://api.ashbyhq.com/posting-api/job-board/{slug}", lambda d: isinstance(d.get("jobs"), list)
    yield "smartrecruiters", {"company": slug}, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1", \
        lambda d: int(d.get("totalFound") or 0) > 0
    yield "workable", {"account": slug}, f"https://apply.workable.com/api/v1/widget/accounts/{slug}", lambda d: isinstance(d.get("jobs"), list)
    if recruitee:  # sub-domain per employer → a wrong guess is a slow DNS failure, so only when asked for
        yield "recruitee", {"subdomain": slug}, f"https://{slug}.recruitee.com/api/offers/", lambda d: isinstance(d.get("offers"), list)


@register("auto")
class AutoATS(Adapter):
    def discover(self, company) -> tuple[str, dict] | None:
        slugs = slug_candidates(company.get("name", ""), self.cfg(company, "slugs", [], required=False))
        for slug in slugs:
            for ats, conf, url, ok in _probes(slug, bool(self.cfg(company, "recruitee", False, required=False))):
                try:
                    data = self.http.get_json(url)
                except HttpError as e:
                    if e.status == 0 and "budget" in str(e):
                        raise
                    continue
                except (ValueError, NotModified):
                    continue
                try:
                    if ok(data):
                        return ats, conf
                except (AttributeError, TypeError, ValueError):
                    continue
        return None

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        st = ctx.state
        found = st.get("found")
        if not found and time.time() - float(st.get("probed_at") or 0) > REPROBE_SECONDS:
            hit = self.discover(company)
            st["probed_at"] = time.time()
            if hit:
                st["found"] = found = {"ats": hit[0], "ats_config": hit[1]}
        if not found:
            return AdapterResult([], complete=False, notes="no public ATS feed found for this employer (re-checked weekly)",
                                 state_out=st)
        inner = dict(company, ats=found["ats"], ats_config={**(company.get("ats_config") or {}), **found["ats_config"]})
        try:
            res = get_adapter(found["ats"], self.http).fetch(inner, ctx)
        except HttpError as e:
            if e.status == 404:  # employer moved to another ATS → find it again next run
                st.pop("found", None); st["probed_at"] = 0
            raise
        res.state_out = {**st, **(res.state_out or {})}
        for j in res.jobs:
            j.source = "auto"  # stable key for this source in the database
        res.notes = (res.notes + " · " if getattr(res, "notes", "") else "") + f"via {found['ats']} ({next(iter(found['ats_config'].values()))})"
        return res

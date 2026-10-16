"""External discovery sources (cross-employer). Only official APIs / permitted channels are used:

* workable_search – Workable's public job-search API (jobs.workable.com). Results ARE employer-hosted postings.
* jooble          – Jooble REST API (free key: https://jooble.org/api/about). Aggregator → unverified leads.
* arbeitnow       – arbeitnow.com free job-board API (Germany / Netherlands, Europe instance). Aggregator → leads.
* email alerts    – see email_alerts.py; ingests the job-alert e-mails LinkedIn / Indeed / Bayt / GulfTalent /
                    Naukrigulf send you, which is the compliant way to cover platforms that forbid scraping.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..adapters.base import Adapter, AdapterResult, FetchContext, RawJob, register
from ..util import html_to_text, iso, parse_dt, sha


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


@register("workable_search")
class WorkableSearch(Adapter):
    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        queries = (company.get("ats_config") or {}).get("queries") or ["data analyst"]
        location = (company.get("ats_config") or {}).get("location", "Dubai")
        jobs: dict[str, RawJob] = {}
        for q in queries:
            token = None
            for _ in range(min(ctx.max_pages, 5)):
                params = {"query": q, "location": location}
                if token:
                    params["pageToken"] = token
                data = self.http.get_json("https://jobs.workable.com/api/v1/jobs", params=params)
                for j in data.get("jobs", []) or data.get("results", []) or []:
                    comp = j.get("company") or {}
                    cname = comp.get("title") or comp.get("name") if isinstance(comp, dict) else str(comp)
                    locs = j.get("locations") or ([j.get("location")] if j.get("location") else [])
                    loc = " | ".join(", ".join(x for x in (l.get("city"), l.get("subregion") or l.get("region"),
                                                           l.get("countryName") or l.get("country")) if x)
                                     if isinstance(l, dict) else str(l) for l in locs)
                    jid = str(_first(j, "id", "shortcode", default=j.get("url")))
                    jobs[jid] = RawJob(source=self.name, external_id=jid, title=j.get("title", ""),
                                       url=_first(j, "url", "applyUrl", "shortlink", default=""), location=loc,
                                       description=html_to_text(_first(j, "description", "requirementsSection", default="")) or None,
                                       employment_type=_first(j, "employmentType", "employment_type"),
                                       posted_at=iso(parse_dt(_first(j, "created", "published", "publishedAt"))),
                                       company_name=cname, is_official=True,
                                       extra={"via": "jobs.workable.com"})
                token = data.get("nextPageToken")
                if not token:
                    break
        return AdapterResult(list(jobs.values()), complete=False)


@register("jooble")
class Jooble(Adapter):
    def fetch(self, company, ctx):
        key = os.environ.get("JOOBLE_API_KEY")
        if not key:
            return AdapterResult([], complete=False, notes="JOOBLE_API_KEY not set – source disabled")
        cfg = company.get("ats_config") or {}
        jobs = {}
        for q in cfg.get("queries", ["data analyst"]):
            data = self.http.post_json(f"https://jooble.org/api/{key}", json_body={"keywords": q, "location": cfg.get("location", "Dubai")})
            for j in data.get("jobs", []):
                jid = str(j.get("id") or sha(j.get("link")))
                jobs[jid] = RawJob(source=self.name, external_id=jid, title=html_to_text(j.get("title")), url=j.get("link", ""),
                                   location=j.get("location", ""), description=html_to_text(j.get("snippet")) or None,
                                   employment_type=j.get("type"), posted_at=iso(parse_dt(j.get("updated"))),
                                   company_name=j.get("company") or None, is_official=False,
                                   extra={"via": "jooble", "board": j.get("source")})
        return AdapterResult(list(jobs.values()), complete=False)


@register("arbeitnow")
class Arbeitnow(Adapter):
    """arbeitnow.com free public job-board API (no key) – mostly Germany / Netherlands, many English, visa-friendly ads.
    Aggregator → unverified leads; the posting links to the employer's own application page."""

    def fetch(self, company, ctx):
        cfg = company.get("ats_config") or {}
        jobs = {}
        for page in range(1, int(cfg.get("pages", 5)) + 1):
            data = self.http.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
            rows = data.get("data") or []
            for j in rows:
                loc = j.get("location") or ""
                if j.get("remote") and "remote" not in loc.lower():
                    loc = (loc + " | Remote").strip(" |")
                if not self.is_uae(loc, None, html_to_text(j.get("description"))[:1500]):
                    continue
                jid = str(j.get("slug") or sha(j.get("url")))
                created = j.get("created_at")
                if isinstance(created, (int, float)):
                    posted = iso(datetime.fromtimestamp(created, tz=timezone.utc))
                else:
                    posted = iso(parse_dt(created)) if created else None
                desc = html_to_text(j.get("description")) or None
                if desc and j.get("visa_sponsorship") is True and "sponsor" not in desc.lower():
                    desc += "\n\n[arbeitnow: visa sponsorship offered]"
                jobs[jid] = RawJob(source=self.name, external_id=jid, title=j.get("title", ""), url=j.get("url", ""),
                                   location=loc, description=desc, employment_type=", ".join(j.get("job_types") or []) or None,
                                   posted_at=posted, company_name=j.get("company_name") or None, is_official=False,
                                   extra={"via": "arbeitnow", "tags": j.get("tags") or []})
            if not rows or not (data.get("links") or {}).get("next"):
                break
        return AdapterResult(list(jobs.values()), complete=False)

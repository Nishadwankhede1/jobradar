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


@register("ba_jobsuche")
class BAJobsuche(Adapter):
    """Germany's Federal Employment Agency (Bundesagentur für Arbeit) job portal – the official national job board,
    where almost every German employer advertises. Search: the public REST API the portal itself uses (documented at
    bund.dev, public client key). Full description: the portal's own job page (robots.txt allows it), only for postings
    not seen before, capped per run. Postings are leads (is_official=False); the employer's own page wins when found."""

    API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
    DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
    KEY = {"X-API-Key": "jobboerse-jobsuche"}

    @staticmethod
    def _detail(page: str) -> dict:
        import json as _json
        import re as _re
        m = _re.search(r'<script id="ng-state"[^>]*>(.*?)</script>', page or "", _re.S)
        if not m:
            return {}
        try:
            return (_json.loads(m.group(1)) or {}).get("jobdetail") or {}
        except ValueError:
            return {}

    @staticmethod
    def _salary(j: dict) -> str:
        lo, hi = j.get("gehaltsspanneVon"), j.get("gehaltsspanneBis")
        if not (lo or hi):
            return ""
        per = "per year" if (j.get("verguetungsangabe") or "").upper().startswith("JAHR") else "per month"
        nums = " - ".join(f"€{int(float(x)):,}" for x in (lo, hi) if x)
        return f"\n\nSalary: {nums} {per}."

    def fetch(self, company, ctx):
        cfg = company.get("ats_config") or {}
        days, size = int(cfg.get("days", 14)), int(cfg.get("size", 50))
        found: dict[str, dict] = {}
        for q in cfg.get("queries") or ["data analyst"]:
            for page in range(1, int(cfg.get("pages", 2)) + 1):
                data = self.http.get_json(self.API, params={"angebotsart": 1, "was": q, "veroeffentlichtseit": days,
                                                           "page": page, "size": size}, headers=self.KEY)
                rows = data.get("ergebnisliste") or []
                for j in rows:
                    if j.get("referenznummer"):
                        found.setdefault(j["referenznummer"], j)
                if len(rows) < size:
                    break
        jobs, fetched, deferred = [], 0, 0
        agencies = set((ctx.state or {}).get("agency") or [])   # remembered: a known posting's page isn't re-read
        for ref, j in found.items():
            adr = ((j.get("stellenlokationen") or [{}])[0] or {}).get("adresse") or {}
            loc = ", ".join(x for x in (adr.get("ort"), "Germany") if x)
            marker = j.get("aenderungsdatum") or ""
            desc = None
            if self.need_detail(ctx, ref, marker):
                if fetched >= ctx.max_detail_fetches:
                    deferred += 1   # picked up on the next run instead of being stored without a description
                    continue
                fetched += 1
                try:
                    d = self._detail(self.http.get(self.DETAIL + ref).text)
                except Exception as e:  # one broken page must not stop the source – retried next run
                    if "budget" in str(e):
                        raise
                    deferred += 1
                    continue
                desc = (html_to_text(d.get("stellenangebotsBeschreibung")) or "") + self._salary(j)
                desc = desc.strip() or None
                if d.get("istPrivateArbeitsvermittlung") or d.get("istArbeitnehmerUeberlassung"):
                    agencies.add(ref)
            agency = ref in agencies
            posted = (j.get("veroeffentlichungszeitraum") or {}).get("von") or j.get("datumErsteVeroeffentlichung")
            jobs.append(RawJob(source=self.name, external_id=ref, title=j.get("stellenangebotsTitel", ""),
                               url=j.get("externeURL") or self.DETAIL + ref, location=loc, description=desc,
                               posted_at=iso(parse_dt(posted)) if posted else None, company_name=j.get("firma") or None,
                               country_code="DE", updated_marker=marker, is_official=False, is_agency=agency,
                               extra={"via": "arbeitsagentur.de"}))
        notes = f"{len(found)} postings, {fetched} new read" + (f", {deferred} left for the next run" if deferred else "")
        keep = sorted(agencies & set(found))[-2000:]
        return AdapterResult(jobs, complete=False, notes=notes, state_out={**(ctx.state or {}), "agency": keep})

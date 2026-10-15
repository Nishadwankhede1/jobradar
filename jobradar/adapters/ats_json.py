"""Adapters for ATS platforms that expose public, documented JSON job-board feeds."""
from __future__ import annotations

import re

from ..util import html_to_text, iso, parse_dt
from .base import Adapter, AdapterResult, FetchContext, RawJob, register


def _iso(v):
    return iso(parse_dt(v)) if v else None


# --------------------------------------------------------------------------- Greenhouse
@register("greenhouse")
class Greenhouse(Adapter):
    """https://developers.greenhouse.io/job-board.html — public Job Board API."""

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        board = self.cfg(company, "board")
        region = self.cfg(company, "region", "global", required=False)
        base = "https://boards-api.eu.greenhouse.io" if region == "eu" else "https://boards-api.greenhouse.io"
        data = self.http.get_json(f"{base}/v1/boards/{board}/jobs", conditional=True)
        jobs, details = [], 0
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "") or ""
            offices = " | ".join(o.get("name", "") for o in j.get("offices", []) or [])
            full_loc = loc if not offices or offices in loc else f"{loc} | {offices}"
            if not self.is_uae(full_loc):
                continue
            jid = str(j["id"])
            marker = j.get("updated_at") or ""
            desc, dept = None, None
            if self.need_detail(ctx, jid, marker) and details < ctx.max_detail_fetches:
                d = self.http.get_json(f"{base}/v1/boards/{board}/jobs/{jid}")
                details += 1
                desc = html_to_text(d.get("content"))
                dept = ", ".join(x.get("name", "") for x in d.get("departments", []) or []) or None
            jobs.append(RawJob(source=self.name, external_id=jid, title=j.get("title", ""), url=j.get("absolute_url", ""),
                               location=full_loc, description=desc, department=dept,
                               posted_at=_iso(j.get("first_published") or j.get("updated_at")),
                               updated_marker=marker))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Lever
@register("lever")
class Lever(Adapter):
    """https://github.com/lever/postings-api — public Postings API."""

    def fetch(self, company, ctx):
        site = self.cfg(company, "site")
        region = self.cfg(company, "region", "global", required=False)
        host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        data = self.http.get_json(f"https://{host}/v0/postings/{site}", params={"mode": "json"}, conditional=True)
        jobs = []
        for p in data if isinstance(data, list) else []:
            cat = p.get("categories") or {}
            locs = [cat.get("location") or ""] + list(cat.get("allLocations") or [])
            loc = " | ".join(dict.fromkeys(x for x in locs if x))
            if not self.is_uae(loc, p.get("country")):
                continue
            parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
            for lst in p.get("lists") or []:
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            parts.append(p.get("additionalPlain") or "")
            jobs.append(RawJob(source=self.name, external_id=p["id"], title=p.get("text", ""),
                               url=p.get("hostedUrl") or p.get("applyUrl", ""), location=loc,
                               description="\n\n".join(x for x in parts if x).strip(),
                               employment_type=cat.get("commitment"), department=cat.get("team") or cat.get("department"),
                               posted_at=_iso(p.get("createdAt")), country_code=p.get("country")))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Ashby
@register("ashby")
class Ashby(Adapter):
    """https://developers.ashbyhq.com/docs/public-job-posting-api"""

    def fetch(self, company, ctx):
        board = self.cfg(company, "board")
        data = self.http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                                  params={"includeCompensation": "false"}, conditional=True)
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            addr = ((j.get("address") or {}).get("postalAddress") or {})
            locs = [j.get("location") or "", addr.get("addressLocality") or "", addr.get("addressCountry") or ""]
            locs += [(s or {}).get("location", "") for s in j.get("secondaryLocations") or []]
            loc = " | ".join(dict.fromkeys(x for x in locs if x))
            if not self.is_uae(loc):
                continue
            jobs.append(RawJob(source=self.name, external_id=j["id"], title=j.get("title", ""),
                               url=j.get("jobUrl") or j.get("applyUrl", ""), location=loc,
                               description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                               employment_type=j.get("employmentType"), department=j.get("department") or j.get("team"),
                               posted_at=_iso(j.get("publishedAt"))))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Workable
@register("workable")
class Workable(Adapter):
    """Workable public widget API (used by employers' own career widgets)."""

    def fetch(self, company, ctx):
        acct = self.cfg(company, "account")
        data = self.http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{acct}",
                                  params={"details": "true"}, conditional=True)
        jobs = []
        for j in data.get("jobs", []):
            locs = [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
            for l in j.get("locations") or []:
                locs.append(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x))
            loc = " | ".join(dict.fromkeys(x for x in locs if x))
            ccs = {(l.get("countryCode") or "").upper() for l in j.get("locations") or []}
            if not (self.is_uae(loc) or "AE" in ccs):
                continue
            jobs.append(RawJob(source=self.name, external_id=j.get("shortcode") or j.get("code") or j["url"],
                               title=j.get("title", ""), url=j.get("url") or j.get("application_url", ""), location=loc,
                               description=html_to_text(j.get("description")) or None,
                               employment_type=j.get("employment_type"), department=j.get("department"),
                               posted_at=_iso(j.get("published_on") or j.get("created_at")),
                               country_code="AE" if "AE" in ccs else None))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Recruitee
@register("recruitee")
class Recruitee(Adapter):
    def fetch(self, company, ctx):
        sub = self.cfg(company, "subdomain")
        data = self.http.get_json(f"https://{sub}.recruitee.com/api/offers/", conditional=True)
        jobs = []
        for o in data.get("offers", []):
            loc = o.get("location") or ", ".join(x for x in (o.get("city"), o.get("country")) if x)
            if not self.is_uae(loc, o.get("country_code")):
                continue
            desc = html_to_text(o.get("description")) + "\n\n" + html_to_text(o.get("requirements"))
            jobs.append(RawJob(source=self.name, external_id=str(o["id"]), title=o.get("title", ""),
                               url=o.get("careers_url", ""), location=loc, description=desc.strip(),
                               employment_type=o.get("employment_type_code"), department=o.get("department"),
                               posted_at=_iso(o.get("published_at")), country_code=o.get("country_code")))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- BambooHR
@register("bamboohr")
class BambooHR(Adapter):
    def fetch(self, company, ctx):
        sub = self.cfg(company, "subdomain")
        base = f"https://{sub}.bamboohr.com/careers"
        data = self.http.get_json(f"{base}/list")
        jobs, details = [], 0
        for j in data.get("result", []):
            l1, l2 = j.get("location") or {}, j.get("atsLocation") or {}
            loc = ", ".join(x for x in (l1.get("city"), l1.get("state"), l2.get("city"), l2.get("country")) if x)
            if not self.is_uae(loc):
                continue
            jid = str(j["id"])
            desc, url, posted = None, f"{base}/{jid}", None
            if self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches:
                d = (self.http.get_json(f"{base}/{jid}/detail").get("result") or {}).get("jobOpening") or {}
                details += 1
                desc, url, posted = html_to_text(d.get("description")), d.get("jobOpeningShareUrl") or url, _iso(d.get("datePosted"))
            jobs.append(RawJob(source=self.name, external_id=jid, title=j.get("jobOpeningName", ""), url=url,
                               location=loc, description=desc, employment_type=j.get("employmentStatusLabel"),
                               department=j.get("departmentLabel"), posted_at=posted))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Pinpoint
@register("pinpoint")
class Pinpoint(Adapter):
    def fetch(self, company, ctx):
        url = self.cfg(company, "feed_url", required=False) or f"https://{self.cfg(company, 'subdomain')}.pinpointhq.com/postings.json"
        data = self.http.get_json(url, conditional=True)
        jobs = []
        for j in data.get("data", data if isinstance(data, list) else []):
            l = j.get("location") or {}
            if isinstance(l, dict):
                loc = ", ".join(dict.fromkeys(x for x in (l.get("city"), l.get("province"), l.get("name"), l.get("country")) if x))
            else:
                loc = str(l)
            if not self.is_uae(loc or ""):
                continue
            desc = "\n\n".join(html_to_text(j.get(k)) for k in ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits") if j.get(k))
            jobs.append(RawJob(source=self.name, external_id=str(j.get("id")), title=j.get("title", ""), url=j.get("url", ""),
                               location=loc or "", description=desc or None, employment_type=j.get("employment_type_text"),
                               department=(j.get("department") or {}).get("name") if isinstance(j.get("department"), dict) else j.get("department"),
                               posted_at=_iso(j.get("created_at") or j.get("published_at"))))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Amazon
@register("amazon")
class Amazon(Adapter):
    """amazon.jobs public search JSON (the endpoint behind the official careers site)."""

    def fetch(self, company, ctx):
        jobs, offset, hits = [], 0, None
        for _ in range(ctx.max_pages):
            data = self.http.get_json("https://www.amazon.jobs/en/search.json", params={
                "normalized_country_code[]": "ARE", "result_limit": 100, "offset": offset, "sort": "recent"})
            hits = data.get("hits", 0)
            page = data.get("jobs", [])
            for j in page:
                loc = j.get("normalized_location") or j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country_code")) if x)
                if not self.is_uae(loc, "AE" if j.get("country_code") in ("ARE", "AE") else None):
                    continue
                desc = "\n\n".join(x for x in (
                    html_to_text(j.get("description")),
                    "BASIC QUALIFICATIONS\n" + html_to_text(j.get("basic_qualifications")) if j.get("basic_qualifications") else "",
                    "PREFERRED QUALIFICATIONS\n" + html_to_text(j.get("preferred_qualifications")) if j.get("preferred_qualifications") else "") if x)
                jobs.append(RawJob(source=self.name, external_id=str(j.get("id_icims") or j.get("id")), title=j.get("title", ""),
                                   url="https://www.amazon.jobs" + j.get("job_path", ""), location=loc, description=desc,
                                   employment_type=j.get("job_schedule_type"), department=j.get("business_category") or j.get("job_category"),
                                   posted_at=_iso(j.get("posted_date")), company_name=j.get("company_name") or None, country_code="AE"))
            offset += len(page)
            if not page or offset >= hits:
                break
        return AdapterResult(jobs, complete=hits is not None and offset >= (hits or 0))


# --------------------------------------------------------------------------- SmartRecruiters
@register("smartrecruiters")
class SmartRecruiters(Adapter):
    """https://developers.smartrecruiters.com/docs/posting-api — public Posting API."""

    def fetch(self, company, ctx):
        cid = self.cfg(company, "company")
        brands = [b.lower() for b in self.cfg(company, "brand_keywords", [], required=False)]
        brand_only = bool(self.cfg(company, "brand_filter", False, required=False))
        base = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
        jobs, offset, total, details = [], 0, None, 0
        for _ in range(ctx.max_pages):
            data = self.http.get_json(base, params={"country": "ae", "limit": 100, "offset": offset})
            total = data.get("totalFound", 0)
            page = data.get("content", [])
            for p in page:
                l = p.get("location") or {}
                loc = l.get("fullLocation") or ", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
                if not self.is_uae(loc, l.get("country")):
                    continue
                pid = str(p["id"])
                marker = p.get("releasedDate") or ""
                desc, url = None, f"https://jobs.smartrecruiters.com/{cid}/{pid}"
                if self.need_detail(ctx, pid, marker) and details < ctx.max_detail_fetches:
                    d = self.http.get_json(f"{base}/{pid}")
                    details += 1
                    secs = ((d.get("jobAd") or {}).get("sections") or {})
                    desc = "\n\n".join(f"{(s or {}).get('title', '')}\n{html_to_text((s or {}).get('text'))}"
                                       for k, s in secs.items() if k != "companyDescription" and s)
                    url = d.get("postingUrl") or url
                brand = None
                hay = f"{p.get('name', '')} {(p.get('department') or {}).get('label', '')} {desc or ''}".lower()
                for b in brands:
                    if re.search(rf"\b{re.escape(b)}\b", hay):
                        brand = b.title()
                        break
                if brand_only and brands and not brand and desc is not None:
                    continue
                jobs.append(RawJob(source=self.name, external_id=pid, title=p.get("name", ""), url=url, location=loc,
                                   description=desc, employment_type=(p.get("typeOfEmployment") or {}).get("label"),
                                   department=(p.get("department") or {}).get("label"), posted_at=_iso(p.get("releasedDate")),
                                   company_name=brand, country_code=(l.get("country") or "").upper() or None,
                                   updated_marker=marker))
            offset += len(page)
            if not page or offset >= total:
                break
        return AdapterResult(jobs, complete=total is not None and offset >= (total or 0))

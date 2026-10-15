"""Adapters for career sites without a JSON API: SAP SuccessFactors CSB, schema.org JobPosting pages,
Teamtailor RSS and Personio XML. These respect robots.txt and fetch detail pages only for new postings."""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from ..location import UAE_RX
from ..util import html_to_text, iso, norm_space, parse_dt
from .base import Adapter, AdapterResult, FetchContext, RawJob, register

LD_RX = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
A_RX = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)


def extract_jobposting(page_html: str) -> dict | None:
    """Return the first schema.org JobPosting object embedded in a page."""
    for block in LD_RX.findall(page_html or ""):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", block.strip()))
            except json.JSONDecodeError:
                continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if isinstance(d, dict):
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return d
                stack.extend(d.get("@graph", []) if isinstance(d.get("@graph"), list) else [])
            elif isinstance(d, list):
                stack.extend(d)
    return None


def jobposting_location(jp: dict) -> tuple[str, str | None]:
    locs, cc = [], None
    jl = jp.get("jobLocation") or []
    for l in jl if isinstance(jl, list) else [jl]:
        a = (l or {}).get("address") or {}
        if isinstance(a, str):
            locs.append(a)
            continue
        country = a.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        locs.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), country) if x))
        if country and len(str(country)) <= 3:
            cc = str(country).upper()
    if jp.get("jobLocationType") == "TELECOMMUTE":
        locs.append("Remote")
    return " | ".join(x for x in locs if x), cc


def rawjob_from_jobposting(jp: dict, url: str, source: str, fallback_id: str) -> RawJob:
    loc, cc = jobposting_location(jp)
    emp = jp.get("employmentType")
    ident = jp.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    org = jp.get("hiringOrganization")
    return RawJob(source=source, external_id=str(ident or fallback_id), title=htmllib.unescape(jp.get("title", "")),
                  url=jp.get("url") or url, location=loc, description=html_to_text(jp.get("description")),
                  employment_type=", ".join(emp) if isinstance(emp, list) else emp,
                  posted_at=iso(parse_dt(jp.get("datePosted"))), country_code=cc,
                  company_name=(org.get("name") if isinstance(org, dict) else None),
                  extra={"valid_through": jp.get("validThrough")})


# --------------------------------------------------------------------------- SuccessFactors
@register("successfactors")
class SuccessFactors(Adapter):
    """SAP SuccessFactors Career Site Builder (hosts like jobs.sap.com, careers.ey.com, apply.careers.hsbc.com)."""
    html_scraper = True

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        host = self.cfg(company, "host")
        prefix = (self.cfg(company, "path_prefix", "", required=False) or "").rstrip("/")
        query = self.cfg(company, "location_query", None, required=False)
        base = f"https://{host}{prefix}"
        # `optionsFacetsDD_country=AE` is a real country filter; `locationsearch` is loose (EY returned 1,096 global
        # jobs). A few sites (jobs.sap.com) ignore the facet, so config can switch to a precise location_query.
        search = {"q": "", "locationsearch": query} if query else {"q": "", "optionsFacetsDD_country": "AE"}
        rows, startrow, total = [], 0, None
        for _ in range(ctx.max_pages):
            r = self.http.get(f"{base}/search/", params={**search, "startrow": startrow}, check_robots=True)
            page = r.text
            if total is None:
                m = (re.search(r"of\s*<b>\s*(\d+)\s*</b>", page) or re.search(r"\bof\s+(\d+)\s+(?:jobs|results)\b", page, re.I)
                     or re.search(r"(\d+)\s+(?:results|jobs)\b", page, re.I))
                total = int(m.group(1)) if m else None
            found = 0
            for tr in re.findall(r'<tr[^>]*class="[^"]*data-row[^"]*"[^>]*>(.*?)</tr>', page, re.S | re.I):
                a = re.search(r'<a[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tr, re.S | re.I) \
                    or re.search(r'<a[^>]*href="([^"]*/job/[^"]+)"[^>]*>(.*?)</a>', tr, re.S | re.I)
                if not a:
                    continue
                loc = re.search(r'class="[^"]*jobLocation[^"]*"[^>]*>(.*?)</span>', tr, re.S | re.I)
                date = re.search(r'class="[^"]*jobDate[^"]*"[^>]*>(.*?)</span>', tr, re.S | re.I)
                rows.append({"href": urllib.parse.urljoin(base + "/", htmllib.unescape(a.group(1))),
                             "title": norm_space(html_to_text(a.group(2))),
                             "loc": norm_space(html_to_text(loc.group(1))) if loc else "",
                             "date": norm_space(html_to_text(date.group(1))) if date else None})
                found += 1
            if not found:  # newer Career Site Builder themes render "job tiles" instead of a results table
                for tile in re.split(r'<li[^>]*class="[^"]*job-tile', page)[1:]:
                    a = re.search(r'<a[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tile, re.S | re.I)
                    if not a:
                        continue
                    loc = re.search(r'section-field location[^>]*>.*?<div[^>]*>(.*?)</div>', tile, re.S | re.I)
                    rows.append({"href": urllib.parse.urljoin(base + "/", htmllib.unescape(a.group(1))),
                                 "title": norm_space(html_to_text(a.group(2))),
                                 "loc": norm_space(html_to_text(loc.group(1))) if loc else "", "date": None})
                    found += 1
            startrow += found
            if not found or (total is not None and startrow >= total):
                break
        seen, jobs, details = set(), [], 0
        for row in rows:
            m = re.search(r"/(\d{5,})/?(?:\?|$)", row["href"])
            jid = m.group(1) if m else row["href"]
            if jid in seen:
                continue
            seen.add(jid)
            if row["loc"] and not UAE_RX.search(row["loc"]) and not re.search(r"\bAE\b", row["loc"]):
                continue
            desc, posted = None, iso(parse_dt(row["date"]))
            if self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches:
                page = self.http.get(row["href"], check_robots=True).text
                details += 1
                jp = extract_jobposting(page)
                if jp:
                    desc = html_to_text(jp.get("description"))
                    posted = iso(parse_dt(jp.get("datePosted"))) or posted
                else:
                    m2 = re.search(r'itemprop="description"[^>]*>(.*?)</(?:span|div)>\s*</(?:span|div)>', page, re.S | re.I) \
                        or re.search(r'class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</div>', page, re.S | re.I)
                    desc = html_to_text(m2.group(1)) if m2 else None
                    md = re.search(r'itemprop="datePosted"[^>]*content="([^"]+)"', page)
                    posted = iso(parse_dt(md.group(1))) if md else posted
            jobs.append(RawJob(source=self.name, external_id=jid, title=row["title"], url=row["href"],
                               location=row["loc"] or query or "United Arab Emirates", description=desc, posted_at=posted))
        complete = bool(rows) and (total is None or startrow >= total)
        return AdapterResult(jobs, complete=complete, notes="" if rows else "no result rows parsed")


# --------------------------------------------------------------------------- generic JSON-LD
@register("jsonld")
class JsonLd(Adapter):
    """Generic: listing page → job links → detail pages with schema.org JobPosting (Google-for-Jobs markup)."""
    html_scraper = True

    def fetch(self, company, ctx):
        cfg = company.get("ats_config") or {}
        if cfg.get("rss"):
            return TeamtailorRss(self.http).fetch({**company, "ats_config": {"rss": cfg["rss"]}}, ctx)
        listing = self.cfg(company, "listing_url")
        pattern = re.compile(self.cfg(company, "link_pattern"))
        page = self.http.get(listing, check_robots=True).text
        links: dict[str, str] = {}
        for href, text in A_RX.findall(page):
            absu = urllib.parse.urljoin(listing, htmllib.unescape(href)).split("#")[0]
            if pattern.search(absu) and absu.rstrip("/") != listing.rstrip("/"):
                links.setdefault(absu, norm_space(html_to_text(text)))
        jobs, details = [], 0
        for url, text in links.items():
            if url in ctx.known:
                jobs.append(RawJob(source=self.name, external_id=url, title=text, url=url, description=None))
                continue
            if details >= ctx.max_detail_fetches:
                break
            details += 1
            jp = extract_jobposting(self.http.get(url, check_robots=True).text)
            if jp:
                rj = rawjob_from_jobposting(jp, url, self.name, url)
                rj.external_id = url  # URLs are the only id stable across listing + detail pages
                jobs.append(rj)
            elif text:
                jobs.append(RawJob(source=self.name, external_id=url, title=text, url=url, description=""))
        return AdapterResult(jobs, complete=bool(links) and details < ctx.max_detail_fetches,
                             notes="" if links else "no job links matched (JS-rendered listing?)")


# --------------------------------------------------------------------------- Teamtailor RSS
@register("teamtailor")
class TeamtailorRss(Adapter):
    """Teamtailor career sites publish an official RSS feed at /jobs.rss."""

    def fetch(self, company, ctx):
        cfg = company.get("ats_config") or {}
        url = cfg.get("rss") or f"https://{self.cfg(company, 'host')}/jobs.rss"
        if cfg.get("per_page", True) and "?" not in url:
            url += "?per_page=500"  # default page is only 100 items (Chalhoub has 135)
        root = ET.fromstring(self.http.get(url, conditional=True).body)
        jobs, details = [], 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            loc_bits = []
            for child in item:
                if "location" not in child.tag.split("}")[-1].lower():
                    continue
                for el in child.iter():
                    tag = el.tag.split("}")[-1].lower()
                    if tag in ("city", "country", "location", "name") and (el.text or "").strip():
                        loc_bits.append(el.text.strip())
            loc = " | ".join(dict.fromkeys(loc_bits))
            desc = html_to_text(item.findtext("description"))
            if loc and not UAE_RX.search(loc):
                continue
            if not loc and guid not in ctx.known and details < ctx.max_detail_fetches:
                details += 1
                jp = extract_jobposting(self.http.get(link).text) if link else None
                if jp:
                    loc, _ = jobposting_location(jp)
                    desc = desc or html_to_text(jp.get("description"))
            if loc and not UAE_RX.search(loc):
                continue
            jobs.append(RawJob(source=self.name, external_id=guid, title=title, url=link, location=loc,
                               description=desc or None, posted_at=iso(parse_dt(item.findtext("pubDate")))))
        return AdapterResult(jobs, complete=True)


# --------------------------------------------------------------------------- Personio XML
@register("personio")
class Personio(Adapter):
    def fetch(self, company, ctx):
        sub = self.cfg(company, "subdomain")
        root = ET.fromstring(self.http.get(f"https://{sub}.jobs.personio.de/xml", conditional=True).body)
        jobs = []
        for p in root.iter("position"):
            office = p.findtext("office") or ""
            extra = " ".join(o.text or "" for o in p.iter("additionalOffice"))
            loc = norm_space(f"{office} {extra}")
            if not UAE_RX.search(loc):
                continue
            pid = p.findtext("id") or ""
            desc = "\n\n".join(f"{d.findtext('name') or ''}\n{html_to_text(d.findtext('value'))}" for d in p.iter("jobDescription"))
            jobs.append(RawJob(source=self.name, external_id=pid, title=p.findtext("name") or "",
                               url=f"https://{sub}.jobs.personio.de/job/{pid}", location=loc, description=desc,
                               employment_type=p.findtext("employmentType"), department=p.findtext("department"),
                               posted_at=iso(parse_dt(p.findtext("createdAt")))))
        return AdapterResult(jobs, complete=True)

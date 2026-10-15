"""Connectors for employer career sites that are not one of the classic ATS APIs.

* ``phenom``    – Phenom People sites (careers.<company>.com/<site>/<lang>): the server-rendered search page embeds the
                  result JSON (``phApp.ddo.eagerLoadRefineSearch``) and accepts a ``qcountry`` filter.
* ``jibe``      – iCIMS Jibe career sites: public ``/api/jobs`` JSON used by the site itself.
* ``avature``   – Avature portals: server-rendered ``/SearchJobs`` pages, offset pagination, label/value detail pages.
* ``radancy``   – Radancy (TalentBrew) sites: server-rendered country "location" pages.
* ``sitemap``   – any site whose robots.txt publishes a job sitemap; detail pages are fetched once and cached.
* ``html_list`` – a plain server-rendered listing page with links to job pages.
* ``rss``       – generic RSS/Atom job feeds (WordPress Job Manager, Zoho Recruit …).

All of them check robots.txt, fetch detail pages only for postings they have not stored yet, and never log in.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta

from ..location import UAE_RX
from ..util import html_to_text, iso, norm_space, parse_dt, sha, utcnow
from .base import Adapter, AdapterResult, FetchContext, RawJob, register
from .html_sites import extract_jobposting, jobposting_location

_DEC = json.JSONDecoder()
SCRIPT_RX = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
META_RX = re.compile(r'<meta\b[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']', re.I)
META_RX2 = re.compile(r'<meta\b[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']([^"\']+)["\']', re.I)
HREF_RX = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
LOC_LABEL_RX = re.compile(r"\b(?:job\s+)?location(?:\(s\)|s)?\s*[:\-–]?\s*\n?\s*([^\n]{2,120})", re.I)
CLOSING_RX = re.compile(r"\b(?:closing|application)\s+(?:date|deadline)\s*[:\-–]?\s*([0-9]{1,2}[-/ ][A-Za-z0-9]{2,9}[-/ ][0-9]{2,4}|"
                        r"[A-Za-z]+ [0-9]{1,2},? [0-9]{4})", re.I)


# ------------------------------------------------------------------------------------------------ helpers
def json_after(text: str, key: str, start: int = 0):
    """Decode the JSON value that follows ``"key":`` (or ``key =``) in a page; None if absent/invalid."""
    for pat in (f'"{key}":', f"{key} = ", f"{key}="):
        i = text.find(pat, start)
        if i < 0:
            continue
        j = i + len(pat)
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        try:
            return _DEC.raw_decode(text, j)[0]
        except ValueError:
            continue
    return None


def metas(page: str) -> dict:
    out = {}
    for k, v in META_RX.findall(page or ""):
        out.setdefault(k.lower(), htmllib.unescape(v))
    for v, k in META_RX2.findall(page or ""):
        out.setdefault(k.lower(), htmllib.unescape(v))
    return out


def page_text(page: str) -> str:
    return html_to_text(SCRIPT_RX.sub(" ", page or ""))


def _largest_block(page: str, class_rx: str) -> str:
    """Text of the largest element whose class/id/itemprop matches ``class_rx`` (e.g. job description container)."""
    best = ""
    for m in re.finditer(rf'<(div|section|span|article|td)\b[^>]*(?:class|id|itemprop)=["\'][^"\']*(?:{class_rx})[^"\']*["\'][^>]*>',
                         page, re.I):
        tag, s = m.group(1), m.end()
        depth, i = 1, s
        open_rx, close_rx = re.compile(rf"<{tag}\b", re.I), re.compile(rf"</{tag}\s*>", re.I)
        while depth and i < len(page):
            o, c = open_rx.search(page, i), close_rx.search(page, i)
            if not c:
                break
            if o and o.start() < c.start():
                depth, i = depth + 1, o.end()
            else:
                depth, i = depth - 1, c.end()
        txt = html_to_text(SCRIPT_RX.sub(" ", page[s:i]))
        if len(txt) > len(best):
            best = txt
    return best


def avature_fields(page: str) -> dict:
    """Avature detail pages render label/value pairs (`article__content__view__field`); returns {label: value}."""
    out = {}
    blocks = re.split(r'class="article__content__view__field(?:\s|")', page)
    for b in blocks[1:]:
        lab = re.search(r'field__label[^"]*"[^>]*>(.*?)</div>', b, re.S | re.I)
        val = re.search(r'field__value[^"]*"[^>]*>(.*?)(?:</div>\s*</div>|$)', b, re.S | re.I)
        cls = re.match(r'([^"]*)"', b)
        key = norm_space(html_to_text(lab.group(1))) if lab else ""
        key = key or (cls.group(1).strip() if cls else "")
        if val:
            out.setdefault(key or f"field{len(out)}", html_to_text(SCRIPT_RX.sub(" ", val.group(1))))
    return out


def extract_detail(page: str) -> dict:
    """Best-effort job facts from a public job page: JSON-LD → Phenom/Radancy/Avature markup → labelled text."""
    d = {"title": "", "location": "", "description": "", "posted_at": None, "closing": None, "country_code": None,
         "unit": ""}
    jp = extract_jobposting(page)
    if jp:
        d["title"] = htmllib.unescape(jp.get("title") or "")
        d["description"] = html_to_text(htmllib.unescape(jp.get("description") or "")
                                        if "&lt;" in (jp.get("description") or "") else jp.get("description"))
        d["location"], d["country_code"] = jobposting_location(jp)
        d["posted_at"] = iso(parse_dt(jp.get("datePosted")))
        d["closing"] = jp.get("validThrough")
    m = metas(page)
    # Phenom: embedded jobDetail record has the structured location
    if "phApp.ddo" in page:
        i = page.find('"jobDetail":{"status"')
        jd = json_after(page, "jobDetail", i) if i >= 0 else None
        job = ((jd or {}).get("data") or {}).get("job") if isinstance(jd, dict) else None
        if isinstance(job, dict):
            d["title"] = d["title"] or job.get("title") or ""
            locs = [job.get("cityStateCountry") or job.get("cityCountry") or job.get("address") or ""]
            locs += [x if isinstance(x, str) else (x or {}).get("address", "") for x in job.get("multi_location") or []]
            d["location"] = " | ".join(dict.fromkeys(x for x in locs if x)) or d["location"]
            d["description"] = d["description"] or html_to_text(job.get("description"))
            d["posted_at"] = d["posted_at"] or iso(parse_dt(job.get("postedDate")))
    # Radancy: "Dubai~Dubai~United Arab Emirates"
    if not d["location"] and m.get("gtm_tbcn_location"):
        d["location"] = ", ".join(dict.fromkeys(x for x in m["gtm_tbcn_location"].split("~") if x))
    if not d["location"]:  # SAP SuccessFactors career sites
        sm = re.search(r'class=["\'][^"\']*job(?:Geo)?Location[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>', page, re.S | re.I)
        d["location"] = norm_space(html_to_text(sm.group(1))) if sm else ""
    if not d["description"]:
        d["description"] = _largest_block(page, "ats-description|job-description|jobdescription|description")
    if "article__content__view__field" in page:  # Avature
        f = avature_fields(page)
        for k, v in f.items():
            kl = k.lower()
            if not d["location"] and "location" in kl and v:
                d["location"] = norm_space(v)
            if not d["closing"] and "closing" in kl and v:
                d["closing"] = norm_space(v)
            if not d["posted_at"] and re.search(r"posted|^date$|creation", kl) and v:
                d["posted_at"] = iso(parse_dt(norm_space(v)))
            if re.search(r"business unit|property|hotel", kl) and v and not d["unit"]:
                d["unit"] = norm_space(v)[:100]
            if re.search(r"^(name|job title|title)$", kl) and v and not d["title"]:
                d["title"] = norm_space(v)
        longest = max((v for k, v in f.items() if not re.search(r"location|job id|date|ref", k, re.I)
                       and not (d["location"] and v.strip() == d["location"])), key=len, default="")
        if len(longest) > len(d["description"]):
            d["description"] = longest
    text = page_text(page) if not (d["location"] and d["description"]) or not d["closing"] else ""
    if text:
        if not d["location"]:
            lm = LOC_LABEL_RX.search(text)
            if lm:
                loc = re.split(r"\s{2,}|\b(?:closing|posted|date|job id|requisition|department|category)\b", lm.group(1), maxsplit=1, flags=re.I)[0]
                d["location"] = norm_space(loc).strip(" ,;|-")[:120]
        if not d["closing"]:
            cm = CLOSING_RX.search(text)
            d["closing"] = cm.group(1) if cm else None
        if not d["description"]:
            d["description"] = text[:20000]
    if not d["title"]:
        t = m.get("og:title") or (re.search(r"<title[^>]*>(.*?)</title>", page or "", re.S | re.I) or [None, ""])[1]
        d["title"] = norm_space(htmllib.unescape(t))
    # Phenom titles read "<Job> in <City> | <Category> at <Company>"
    pm = re.match(r"(.+?) in ([^|]+?)\s*\|", d["title"])
    if pm and "phApp" in page:
        d["title"] = pm.group(1).strip()
        d["location"] = d["location"] or pm.group(2).strip()
    d["description"] = (d["description"] or "")[:20000]
    return d


def is_closed(closing: str | None) -> bool:
    dt = parse_dt(closing) if closing else None
    return bool(dt and dt < utcnow() - timedelta(days=1))


def _uae(text: str) -> bool:
    return bool(UAE_RX.search(text or ""))


# ------------------------------------------------------------------------------------------------ Phenom
@register("phenom")
class Phenom(Adapter):
    """Phenom People career sites. config: {"base": "https://careers.x.com/global/en", "country": "United Arab Emirates"}"""
    html_scraper = True

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        base = self.cfg(company, "base").rstrip("/")
        country = self.cfg(company, "country", "United Arab Emirates", required=False)
        params = dict(self.cfg(company, "params", None, required=False) or {"qcountry": country})
        jobs, seen, total, frm, details, pages = [], set(), None, 0, 0, 0
        while pages < int(self.cfg(company, "max_pages", 40, required=False)):
            pages += 1
            page = self.http.get(f"{base}/search-results", params={**params, **({"from": frm} if frm else {})},
                                 check_robots=True).text
            data = json_after(page, "eagerLoadRefineSearch")
            if not isinstance(data, dict):
                if pages == 1:
                    return AdapterResult([], complete=False, notes="search page carried no embedded results (site changed?)")
                break
            total = data.get("totalHits", total)
            items = (data.get("data") or {}).get("jobs") or []
            for it in items:
                jid = str(it.get("jobId") or it.get("reqId") or it.get("jobSeqNo") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = [it.get("cityStateCountry") or it.get("address") or ", ".join(
                    x for x in (it.get("city"), it.get("state"), it.get("country")) if x)]
                locs += [x for x in it.get("multi_location") or [] if isinstance(x, str)]
                loc = " | ".join(dict.fromkeys(x for x in locs if x))
                if not (_uae(loc) or _uae(it.get("country") or "")):
                    continue
                url = f"{base}/job/{jid}"
                desc = None
                if self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches:
                    details += 1
                    det = extract_detail(self.http.get(url, check_robots=True).text)
                    desc = det["description"] or html_to_text(it.get("descriptionTeaser")) or ""
                jobs.append(RawJob(source=self.name, external_id=jid, title=it.get("title") or "", url=url, location=loc,
                                   description=desc, posted_at=iso(parse_dt(it.get("postedDate"))),
                                   department=it.get("category"), employment_type=it.get("type"),
                                   company_name=None, extra={"phenom_company": it.get("company")}))
            frm += len(items)
            if not items or total is None or frm >= int(total):
                break
        complete = total is not None and frm >= int(total)
        return AdapterResult(jobs, complete=complete, notes="" if complete else f"stopped at {frm}/{total}")


# ------------------------------------------------------------------------------------------------ iCIMS Jibe
@register("jibe")
class Jibe(Adapter):
    """iCIMS Jibe sites. config: {"host": "careers.flydubai.com", "params": {"country": "United Arab Emirates"}}"""

    def fetch(self, company, ctx):
        host = self.cfg(company, "host")
        params = dict(self.cfg(company, "params", None, required=False) or {"country": "United Arab Emirates"})
        limit = int(self.cfg(company, "limit", 100, required=False))
        jobs, seen, total = [], 0, None
        for page in range(1, ctx.max_pages + 1):
            r = self.http.get(f"https://{host}/api/jobs", params={**params, "page": page, "limit": limit}).json()
            items = r.get("jobs") or []
            total = r.get("totalCount", total)
            for it in items:
                d = it.get("data") or it
                extra_locs = [x.get("full_location") or x.get("location_name") or "" if isinstance(x, dict) else str(x)
                              for x in d.get("additional_locations") or []]
                loc = d.get("full_location") or d.get("location_name") or ", ".join(
                    x for x in (d.get("city"), d.get("state"), d.get("country")) if x)
                loc = " | ".join(dict.fromkeys(x for x in [loc, *extra_locs] if x))
                if not self.is_uae(loc, d.get("country_code")):
                    continue
                slug = d.get("slug") or d.get("req_id")
                url = (d.get("meta_data") or {}).get("canonical_url") or f"https://{host}/jobs/{slug}?lang={d.get('language') or 'en-us'}"
                desc = "\n\n".join(html_to_text(d.get(k)) for k in ("description", "responsibilities", "qualifications") if d.get(k))
                jobs.append(RawJob(source=self.name, external_id=str(d.get("req_id") or slug), title=d.get("title") or "",
                                   url=url, location=loc, description=desc, country_code=d.get("country_code"),
                                   employment_type=d.get("employment_type"), department=d.get("department") or d.get("category"),
                                   posted_at=iso(parse_dt(d.get("posted_date") or d.get("create_date")))))
            seen += len(items)
            if not items or (total is not None and seen >= int(total)):
                break
        complete = total is None or seen >= int(total)
        return AdapterResult(jobs, complete=complete)


# ------------------------------------------------------------------------------------------------ Avature
AV_LINK_RX = re.compile(r'href=["\']([^"\']*/(?:JobDetail|PipelineDetail|FolderDetail)/[^"\']*)["\'][^>]*>(.*?)</a>', re.S | re.I)
AV_ITEM_RX = re.compile(r'class=["\'][^"\']*list-item-([\w-]+)[^"\']*["\'][^>]*>(.*?)</(?:span|li|div)>', re.S | re.I)


def avature_rows(page: str, base_url: str) -> list[dict]:
    rows = []
    for block in re.split(r'class=["\']article article--result', page)[1:]:
        a = AV_LINK_RX.search(block)
        if not a:
            continue
        href = urllib.parse.urljoin(base_url, htmllib.unescape(a.group(1)))
        idm = re.search(r"/(\d+)/?(?:[?#]|$)", href)
        items = {k.lower(): norm_space(html_to_text(v)) for k, v in AV_ITEM_RX.findall(block)}
        loc = items.get("location") or ", ".join(v for k, v in items.items() if re.search(r"city|country|location", k) and v)
        if not loc:
            sub = re.search(r'__subtitle["\'][^>]*>(.*?)</div>', block, re.S | re.I)
            loc = norm_space(html_to_text(sub.group(1))) if sub else ""
        date = next((v for k, v in items.items() if "date" in k), None)
        rows.append({"id": idm.group(1) if idm else href, "url": href, "title": norm_space(html_to_text(a.group(2))),
                     "loc": loc, "date": date})
    return rows


@register("avature")
class Avature(Adapter):
    """Avature portals. config: {"portal": "https://jobs.siemens.com/en_US/externaljobs",
    "searches": ["United Arab Emirates", "Dubai"], "query_style": "path"|"param", "all_uae": false}"""
    html_scraper = True

    def _search_url(self, portal: str, kw: str, style: str) -> str:
        if not kw:
            return f"{portal}/SearchJobs/"
        return f"{portal}/SearchJobs/?search={urllib.parse.quote(kw)}" if style == "param" \
            else f"{portal}/SearchJobs/{urllib.parse.quote(kw)}/"

    def fetch(self, company, ctx):
        portal = self.cfg(company, "portal").rstrip("/")
        searches = self.cfg(company, "searches", None, required=False) or ["United Arab Emirates", "Dubai"]
        style = self.cfg(company, "query_style", "path", required=False)
        all_uae = bool(self.cfg(company, "all_uae", False, required=False))
        rows: dict[str, dict] = {}
        complete = True
        for kw in searches:
            url, offset = self._search_url(portal, kw, style), 0
            for _ in range(ctx.max_pages * 2):
                page = self.http.get(url, check_robots=True).text
                found = avature_rows(page, url)
                for r in found:
                    rows.setdefault(r["id"], r)
                offset += len(found)
                tm = re.search(r"[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)", page)
                total = int(tm.group(1).replace(",", "")) if tm else None
                nxt = None
                for href in re.findall(r'href=["\']([^"\']*?(\w+)Offset=(\d+)[^"\']*)["\']', page):
                    if int(href[2]) == offset:
                        nxt = urllib.parse.urljoin(url, htmllib.unescape(href[0]))
                        break
                if not found or not nxt or (total is not None and offset >= total):
                    if total is not None and offset < total and found:
                        complete = False
                    break
                url = nxt
        state = dict(ctx.state or {})
        non_uae = set(state.get("non_uae", []))
        jobs, details, undecided = [], 0, 0
        for jid, r in rows.items():
            if jid in non_uae:
                continue
            loc_known = bool(r["loc"]) and not re.search(r"multiple|various|several", r["loc"], re.I)
            if loc_known and not all_uae and not _uae(r["loc"]):
                continue
            if jid in ctx.known:
                jobs.append(RawJob(source=self.name, external_id=jid, title=r["title"], url=r["url"],
                                   location=r["loc"] if loc_known else "", description=None))
                continue
            if details >= ctx.max_detail_fetches:
                undecided += 1
                continue
            details += 1
            det = extract_detail(self.http.get(r["url"], check_robots=True).text)
            loc = (r["loc"] if loc_known else "") or det["location"]
            if all_uae:
                loc = ", ".join(x for x in (det["unit"], loc or "United Arab Emirates") if x)
            elif not (_uae(loc) or (not det["location"] and _uae(r["title"] + " " + r["url"]))):
                non_uae.add(jid)
                continue
            if is_closed(det["closing"]):
                continue
            jobs.append(RawJob(source=self.name, external_id=jid, title=r["title"] or det["title"], url=r["url"],
                               location=loc or r["title"], description=det["description"] or "",
                               posted_at=det["posted_at"] or iso(parse_dt(r["date"])), extra={"closing": det["closing"]}))
        state["non_uae"] = sorted(non_uae & set(rows))[-2000:]
        state["initial_done"] = bool(state.get("initial_done")) or undecided == 0
        return AdapterResult(jobs, complete=complete and undecided == 0 and bool(rows or searches), state_out=state,
                             baseline_ready=state["initial_done"],
                             notes=f"{undecided} postings left for the next run" if undecided else "")


# ------------------------------------------------------------------------------------------------ Radancy
@register("radancy")
class Radancy(Adapter):
    """Radancy / TalentBrew. config: {"url": "https://careers.moodys.com/en/location/united-arab-emirates-jobs/49841/290557/2"}"""
    html_scraper = True

    def fetch(self, company, ctx):
        start = self.cfg(company, "url")
        jobs, seen, details, expected = [], set(), 0, None
        for p in range(1, ctx.max_pages + 1):
            url = start if p == 1 else start + ("&" if "?" in start else "?") + f"p={p}"
            page = self.http.get(url, check_robots=True).text
            if expected is None:
                cm = re.search(r'data-results-count=["\'](\d+)', page) or re.search(r"(\d+)\s+jobs?\s+(?:found|in)\b", page, re.I)
                expected = int(cm.group(1)) if cm else None
            i = max(page.find('id="search-results-jobs"'), page.find('id="search-results-list"'))
            region = page[i:] if i >= 0 else page
            end = re.search(r'class=["\'][^"\']*pagination|<section\b|[Ff]eatured [Jj]obs', region[50:])
            region = region[: end.start() + 50] if end else region
            anchors = [m for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']*/job/[^"\']+)["\'][^>]*>(.*?)</a>', region, re.S | re.I)]
            new = 0
            for k, m in enumerate(anchors):
                href = urllib.parse.urljoin(url, htmllib.unescape(m.group(1)))
                jid = (re.search(r"/(\d{4,})/?(?:[?#]|$)", href) or [None, href])[1]
                if jid in seen:
                    continue
                seen.add(jid)
                new += 1
                tail = region[m.end(): anchors[k + 1].start() if k + 1 < len(anchors) else m.end() + 1500]
                lm = re.search(r'class=["\'][^"\']*job-location[^"\']*["\'][^>]*>(.*?)</', tail, re.S | re.I)
                loc = norm_space(html_to_text(lm.group(1))) if lm else ""
                if loc and not _uae(loc):
                    continue
                desc, posted = None, None
                if self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches:
                    details += 1
                    det = extract_detail(self.http.get(href, check_robots=True).text)
                    desc, posted, loc = det["description"], det["posted_at"], loc or det["location"]
                    if not _uae(loc):
                        continue
                jobs.append(RawJob(source=self.name, external_id=jid, title=norm_space(html_to_text(m.group(2))),
                                   url=href, location=loc, description=desc, posted_at=posted))
            if not new or (expected is not None and len(seen) >= expected):
                break
        complete = expected is None or len(seen) >= expected
        return AdapterResult(jobs, complete=complete)


# ------------------------------------------------------------------------------------------------ Sitemap
@register("sitemap")
class Sitemap(Adapter):
    """Job URLs from a published sitemap + one detail fetch per new posting (cached between runs).

    config: {"sitemap": url | [urls], "link_pattern": regex, "url_include": regex?, "url_exclude": regex?,
             "child_pattern": regex? (for sitemap indexes), "max_age_days": int?, "detail_budget": int?,
             "id_pattern": regex?}
    """
    html_scraper = True

    def _entries(self, url: str, child_rx, depth=0) -> list[tuple[str, str | None]]:
        body = self.http.get(url, check_robots=True).body
        if not (body or b"").strip():
            raise ValueError("sitemap answered with an empty page (the site may be rate-limiting automated requests)")
        try:
            root = ET.fromstring(body)
            tag = root.tag.split("}")[-1]
            nodes = [(next((c.text for c in n if c.tag.split("}")[-1] == "loc"), None),
                      next((c.text for c in n if c.tag.split("}")[-1] == "lastmod"), None)) for n in root]
        except ET.ParseError:  # malformed XML (stray "&", bad characters) – fall back to reading the <loc> entries
            text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
            tag = "sitemapindex" if re.search(r"<sitemapindex\b", text) else "urlset"
            nodes = [(m.group(1), (re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", m.group(0)) or [None, None])[1])
                     for m in re.finditer(r"<(?:url|sitemap)\b[^>]*>.*?<loc>\s*([^<]+?)\s*</loc>.*?</(?:url|sitemap)>", text, re.S)]
        out = []
        for loc, lm in nodes:
            if not loc:
                continue
            loc = htmllib.unescape(loc.strip())
            if tag == "sitemapindex":
                if depth < 2 and (child_rx is None or child_rx.search(loc)):
                    out += self._entries(loc, child_rx, depth + 1)
            else:
                out.append((loc, lm))
        return out

    def fetch(self, company, ctx):
        maps = self.cfg(company, "sitemap")
        maps = [maps] if isinstance(maps, str) else maps
        link_rx = re.compile(self.cfg(company, "link_pattern"))
        inc = self.cfg(company, "url_include", None, required=False)
        exc = self.cfg(company, "url_exclude", None, required=False)
        inc_rx, exc_rx = (re.compile(inc, re.I) if inc else None), (re.compile(exc, re.I) if exc else None)
        child = self.cfg(company, "child_pattern", None, required=False)
        id_rx = re.compile(self.cfg(company, "id_pattern", r"/(\d{3,})/?(?:[?#]|$)", required=False))
        max_age = self.cfg(company, "max_age_days", None, required=False)
        budget = int(self.cfg(company, "detail_budget", ctx.max_detail_fetches, required=False))
        entries: dict[str, str | None] = {}
        raw = 0
        for m in maps:
            for loc, lm in self._entries(m, re.compile(child) if child else None):
                raw += 1
                if link_rx.search(loc) and (not inc_rx or inc_rx.search(loc)) and not (exc_rx and exc_rx.search(loc)):
                    if max_age and lm and parse_dt(lm) and parse_dt(lm) < utcnow() - timedelta(days=int(max_age)):
                        continue
                    entries.setdefault(loc, lm)
        state = dict(ctx.state or {})
        cache: dict = state.get("pages", {})
        new_cache, jobs, details, pending = {}, [], 0, 0
        for url, lm in entries.items():
            idm = id_rx.search(url)
            jid = idm.group(1) if idm else url
            c = cache.get(url)
            fresh = c is not None and (c.get("m") == lm or not lm)
            if fresh and (not c.get("u") or jid in ctx.known):
                new_cache[url] = c
                if c.get("u") and not is_closed(c.get("c")):
                    jobs.append(RawJob(source=self.name, external_id=jid, title=c.get("t") or "", url=url,
                                       location=c.get("l") or "", description=None))
                continue
            if details >= budget:
                pending += 1
                if c is not None:
                    new_cache[url] = c
                continue
            details += 1
            try:
                det = extract_detail(self.http.get(url, check_robots=True).text)
            except Exception as e:  # one dead job page must not fail the whole source
                if "404" in str(e) or "410" in str(e):
                    continue
                raise
            in_uae = _uae(det["location"]) or (not det["location"] and (_uae(det["title"]) or _uae(det["description"][:3000])))
            new_cache[url] = {"t": det["title"], "l": det["location"], "u": in_uae, "m": lm, "c": det["closing"]}
            if in_uae and not is_closed(det["closing"]):
                jobs.append(RawJob(source=self.name, external_id=jid, title=det["title"], url=url,
                                   location=det["location"] or "United Arab Emirates (per job description)",
                                   description=det["description"], posted_at=det["posted_at"],
                                   country_code=det["country_code"], extra={"closing": det["closing"]}))
        state["pages"] = new_cache
        state["initial_done"] = bool(state.get("initial_done")) or pending == 0
        # an empty/unparseable sitemap is never treated as "all jobs closed"
        return AdapterResult(jobs, complete=pending == 0 and raw > 0, state_out=state,
                             baseline_ready=state["initial_done"],
                             notes=f"{pending} job pages left for later runs" if pending else
                             ("" if raw else "sitemap listed no URLs"))


# ------------------------------------------------------------------------------------------------ HTML list
@register("html_list")
class HtmlList(Adapter):
    """A server-rendered list of job links. config: {"urls": [..], "link_pattern": regex, "assume_uae": false,
    "region_start": regex?, "region_end": regex?, "detail": true, "id_pattern": regex?}"""
    html_scraper = True

    def fetch(self, company, ctx):
        urls = self.cfg(company, "urls")
        urls = [urls] if isinstance(urls, str) else urls
        link_rx = re.compile(self.cfg(company, "link_pattern"), re.I)
        rs, re_ = self.cfg(company, "region_start", None, required=False), self.cfg(company, "region_end", None, required=False)
        assume = bool(self.cfg(company, "assume_uae", False, required=False))
        want_detail = self.cfg(company, "detail", True, required=False)
        id_rx = re.compile(self.cfg(company, "id_pattern", r"[/=](\d{3,})(?:[-/?#&]|$)", required=False))
        list_loc = bool(self.cfg(company, "list_location", False, required=False))
        min_count = self.cfg(company, "skip_if_heading", None, required=False)
        rows: dict[str, dict] = {}
        for lu in urls:
            page = self.http.get(lu, check_robots=True).text
            full_page = page
            if min_count and re.search(min_count, page, re.I):
                continue  # e.g. BNP shows "0 job offers" followed by unrelated suggestions
            if rs:
                m = re.search(rs, page, re.I | re.S)
                page = page[m.start():] if m else page
            if re_:
                m = re.search(re_, page[200:], re.I | re.S)
                page = page[: m.start() + 200] if m else page
            bm = re.search(r'<base\b[^>]*href=["\']([^"\']+)["\']', full_page, re.I)
            join_base = urllib.parse.urljoin(lu, bm.group(1)) if bm else lu
            anchors = [(urllib.parse.urljoin(join_base, htmllib.unescape(h)).split("#")[0], t, mm.start(), mm.end())
                       for mm in HREF_RX.finditer(page) for h, t in [mm.groups()]]
            anchors = [a for a in anchors if link_rx.search(a[0]) and a[0].rstrip("/") != lu.rstrip("/")]
            for k, (href, text, s, e) in enumerate(anchors):
                ctx_html = page[e: anchors[k + 1][2] if k + 1 < len(anchors) else e + 1200]
                r = rows.setdefault(href, {"url": href, "title": "", "ctx": ""})
                t = norm_space(html_to_text(text))
                if len(t) > len(r["title"]) and not re.fullmatch(r"(apply|learn more|view|details|read more).*", t, re.I):
                    r["title"] = t
                r["ctx"] += " " + norm_space(html_to_text(ctx_html))[:300]
        jobs, details, pending = [], 0, 0
        for href, r in rows.items():
            idm = id_rx.search(href)
            jid = idm.group(1) if idm else href
            loc_m = UAE_RX.search(r["ctx"]) or UAE_RX.search(r["title"])
            loc = ""
            if loc_m:
                seg = re.split(r"\s[|•·]\s|\s{2,}", r["ctx"])
                loc = next((norm_space(x)[:100] for x in seg if UAE_RX.search(x)), loc_m.group(0))
            if not (loc or assume) and (list_loc or not want_detail):
                continue  # the listing shows each job's location and this one is not in the UAE
            desc, posted = None, None
            if want_detail and self.need_detail(ctx, jid, None):
                if details >= ctx.max_detail_fetches:
                    pending += 1
                    continue
                details += 1
                det = extract_detail(self.http.get(href, check_robots=True).text)
                desc, posted = det["description"], det["posted_at"]
                loc = loc or det["location"]
                r["title"] = r["title"] or det["title"]
                if is_closed(det["closing"]):
                    continue
            elif not want_detail:
                desc = ""
            if not (assume or _uae(loc) or (desc and not loc and _uae(desc[:3000]))):
                continue
            r["title"] = re.sub(r"\s+[—|-]\s+[^—|]*careers?$", "", r["title"], flags=re.I).strip()
            jobs.append(RawJob(source=self.name, external_id=jid, title=r["title"], url=href,
                               location=loc or "United Arab Emirates", description=desc, posted_at=posted))
        state = dict(ctx.state or {})
        state["initial_done"] = bool(state.get("initial_done")) or pending == 0
        return AdapterResult(jobs, complete=pending == 0 and bool(rows), state_out=state, baseline_ready=state["initial_done"],
                             notes=f"{pending} job pages left for later runs" if pending else ("" if rows else "no job links matched"))


# ------------------------------------------------------------------------------------------------ RSS
@register("rss")
class Rss(Adapter):
    """Generic RSS 2.0 / Atom job feed. config: {"url": feed, "assume_uae": false}"""

    def fetch(self, company, ctx):
        url = self.cfg(company, "url")
        assume = bool(self.cfg(company, "assume_uae", False, required=False))
        root = ET.fromstring(self.http.get(url, conditional=True).body)
        items = [n for n in root.iter() if n.tag.split("}")[-1] in ("item", "entry")]
        jobs = []
        for it in items:
            f = {}
            for c in it:
                t = c.tag.split("}")[-1].lower()
                prefix = c.tag.split("}")[0].lower() if "}" in c.tag else ""
                key = ("joblisting_" if "job" in prefix else "") + t
                f.setdefault(key, (c.text or "").strip() or c.attrib.get("href", ""))
            title = htmllib.unescape(f.get("title", ""))
            link = f.get("link") or f.get("guid") or ""
            desc = html_to_text(f.get("encoded") or f.get("description") or f.get("content") or f.get("summary"))
            loc = " | ".join(v for k, v in f.items() if "location" in k and v)
            if not (assume or _uae(loc) or (not loc and (_uae(title) or _uae(desc[:2000])))):
                continue
            jobs.append(RawJob(source=self.name, external_id=f.get("guid") or link, title=title, url=link,
                               location=loc or ("United Arab Emirates" if assume else ""), description=desc,
                               posted_at=iso(parse_dt(f.get("pubdate") or f.get("published") or f.get("updated")))))
        return AdapterResult(jobs, complete=True)


# ------------------------------------------------------------------------------------------------ Oracle Taleo
def _taleo_body(page_no: int, orgs: list[str] | None = None, keyword: str = "") -> dict:
    sel = lambda fid, vals=None: {"id": fid, "selectedValues": list(vals or [])}  # noqa: E731
    return {"multilineEnabled": False,
            "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}, "valid": True},
            "filterSelectionParam": {"searchFilterSelections": [sel("POSTING_DATE"), sel("LOCATION"), sel("JOB_FIELD"),
                                                                sel("ORGANIZATION", orgs)]},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
                sel("ORGANIZATION"), sel("LOCATION"), sel("JOB_FIELD"), sel("URGENT_JOB"), sel("EMPLOYEE_STATUS"),
                sel("STUDY_LEVEL"), sel("WILL_TRAVEL"), sel("JOB_SHIFT"), sel("JOB_NUMBER")]},
            "pageNo": page_no}


def taleo_description(page: str) -> str:
    """Taleo job pages keep the requisition text URL-encoded in the hidden `initialHistory` field."""
    m = re.search(r'id="initialHistory"[^>]*value="([^"]*)"', page or "")
    if not m:
        return _largest_block(page or "", "requisitionDescription|description")
    raw = urllib.parse.unquote_plus(htmllib.unescape(m.group(1)))
    parts = re.split(r"!\|!|!\*!|!\$!", raw)
    best = max(parts, key=len, default="")
    return html_to_text(htmllib.unescape(best.replace("\\:", ":")))[:20000]


@register("taleo")
class Taleo(Adapter):
    """Oracle Taleo Enterprise career sections (JSON used by the job-search page).

    config: {"host": "jobs.dubaicareers.ae", "section": "dubaicareers", "portal": "40505100456",
             "organizations": [ids]?, "exclude_organizations": [ids]?, "location": "Dubai, United Arab Emirates"}
    """

    def _search(self, host, portal, orgs, ctx, keyword: str = "") -> tuple[list[dict], int | None]:
        rows, total = [], None
        for page_no in range(1, ctx.max_pages + 1):
            d = self.http.post(f"https://{host}/careersection/rest/jobboard/searchjobs",
                               params={"lang": "en", "portal": portal}, json_body=_taleo_body(page_no, orgs, keyword),
                               headers={"tz": "GMT+04:00", "tzname": "Asia/Dubai"}).json()
            items = d.get("requisitionList") or []
            total = (d.get("pagingData") or {}).get("totalCount", total)
            rows += items
            if not items or (total is not None and len(rows) >= int(total)):
                break
        return rows, total

    def fetch(self, company, ctx):
        host, section, portal = self.cfg(company, "host"), self.cfg(company, "section"), str(self.cfg(company, "portal"))
        orgs = [str(x) for x in self.cfg(company, "organizations", None, required=False) or []] or None
        excl = [str(x) for x in self.cfg(company, "exclude_organizations", None, required=False) or []]
        default_loc = self.cfg(company, "location", "Dubai, United Arab Emirates", required=False)
        keywords = self.cfg(company, "keywords", None, required=False) or [""]
        detail_tmpl = self.cfg(company, "detail_url", f"https://{host}/careersection/{section}/jobdetail.ftl?job={{job}}&lang=en",
                               required=False)
        want_detail = self.cfg(company, "detail", True, required=False)
        rows, total, complete = [], 0, True
        for kw in keywords:
            r_kw, t_kw = self._search(host, portal, orgs, ctx, kw)
            rows += r_kw
            complete = complete and t_kw is not None and len(r_kw) >= int(t_kw)
        skip = set()
        for org in excl:  # those employers have their own source → avoid double alerts
            skip |= {str(r.get("contestNo")) for r in self._search(host, portal, [org], ctx)[0]}
        jobs, details, seen_c = [], 0, set()
        for r in rows:
            cno = str(r.get("contestNo") or r.get("jobId"))
            if cno in skip:
                continue
            if cno in seen_c:
                continue
            seen_c.add(cno)
            cols = [htmllib.unescape(str(c)) for c in r.get("column") or []]
            title = cols[0] if cols else ""
            loc_col = next((c for c in cols[1:] if c.startswith("[")), None)
            loc = default_loc
            if loc_col:
                try:
                    loc = " | ".join(json.loads(loc_col)) or default_loc
                except ValueError:
                    pass
                if not (re.search(r"\bAE-", loc) or _uae(loc)):
                    continue
                loc = re.sub(r"\bAE-DU-", "Dubai, United Arab Emirates – ", loc).replace("AE-AZ-", "Abu Dhabi, United Arab Emirates – ")
            nationality = next((c for c in cols[2:] if re.search(r"nationalit|uae only|emirati", c, re.I)), "")
            note = ""
            if re.search(r"\bUAE only\b", nationality, re.I):
                note = "Eligibility (Dubai Careers listing): UAE Nationals Only\n\n"
            elif re.search(r"priority for uae", nationality, re.I):
                note = "Eligibility (Dubai Careers listing): all nationalities, UAE nationals preferred\n\n"
            url = detail_tmpl.format(job=cno)
            desc = None if want_detail else note
            if want_detail and self.need_detail(ctx, cno, None) and details < ctx.max_detail_fetches:
                details += 1
                desc = note + taleo_description(self.http.get(url).text)
            posted = next((iso(parse_dt(c)) for c in cols[1:] if parse_dt(c)), None)
            jobs.append(RawJob(source=self.name, external_id=cno, title=title, url=url, location=loc,
                               description=desc, posted_at=posted,
                               extra={"nationality": nationality}))
        return AdapterResult(jobs, complete=complete)


# ------------------------------------------------------------------------------------------------ generic JSON API
def _path(obj, path: str | None, default=None):
    """Tiny JSON path: "a.b.0.c"; "a[].b" maps over a list; "" returns obj."""
    if path in (None, ""):
        return obj
    cur = obj
    parts = path.split(".")
    for i, p in enumerate(parts):
        if p.endswith("[]"):
            seq = (cur or {}).get(p[:-2]) if isinstance(cur, dict) else None
            rest = ".".join(parts[i + 1:])
            return [_path(x, rest) for x in (seq or [])]
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
        if cur is None:
            return default
    return cur


def _flat(v) -> str:
    if isinstance(v, list):
        return " | ".join(dict.fromkeys(_flat(x) for x in v if x))
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if isinstance(x, (str, int)) and x)
    return "" if v is None else str(v)


@register("json_api")
class JsonApi(Adapter):
    """The public JSON endpoint a careers page itself calls (no auth, robots-checked).

    config: {"url": "https://host/api/jobs", "params": {...}, "items": "docs", "total": "numFound",
             "page_param": "page"|"start"|"offset", "page_start": 1, "page_size_param": "pageSize", "page_size": 100,
             "fields": {"id": "jobID", "title": "title", "location": "cities", "description": ["whatYouWillDo", ...],
                        "posted": "created_at"},
             "url_template": "https://host/jobs/{id}", "url_field": "link", "assume_uae": false}
    """

    def fetch(self, company, ctx):
        url = self.cfg(company, "url")
        params = dict(self.cfg(company, "params", None, required=False) or {})
        f = self.cfg(company, "fields")
        items_path = self.cfg(company, "items", "", required=False)
        total_path = self.cfg(company, "total", None, required=False)
        page_param = self.cfg(company, "page_param", None, required=False)
        size_param = self.cfg(company, "page_size_param", None, required=False)
        size = int(self.cfg(company, "page_size", 50, required=False))
        start = int(self.cfg(company, "page_start", 1, required=False))
        offset_mode = page_param in ("start", "offset", "from")
        assume = bool(self.cfg(company, "assume_uae", False, required=False))
        tmpl = self.cfg(company, "url_template", None, required=False)
        rows, total, cursor = [], None, start
        for _ in range(ctx.max_pages):
            p = dict(params)
            if page_param:
                p[page_param] = cursor
            if size_param:
                p[size_param] = size
            data = self.http.get(url, params=p, check_robots=True).json()
            items = _path(data, items_path) or []
            if total_path:
                total = _path(data, total_path, total)
            rows += items
            if not page_param or not items or len(items) < (size if size_param else 1) \
                    or (total is not None and len(rows) >= int(total)):
                break
            cursor = cursor + (len(items) if offset_mode else 1)
        jobs, seen = [], set()
        for it in rows:
            jid = _flat(_path(it, f.get("id")))
            if not jid or jid in seen:
                continue
            seen.add(jid)
            loc = _flat(_path(it, f.get("location")))
            if f.get("country"):
                loc = " | ".join(x for x in (loc, _flat(_path(it, f["country"]))) if x)
            title = htmllib.unescape(_flat(_path(it, f.get("title"))))
            if not (assume or _uae(loc) or (not loc and _uae(title))):
                continue
            descs = f.get("description") or []
            descs = [descs] if isinstance(descs, str) else descs
            desc = "\n\n".join(html_to_text(_flat(_path(it, d))) for d in descs if _path(it, d))
            link = _flat(_path(it, f.get("url"))) if f.get("url") else ""
            if not link and tmpl:
                link = tmpl.format(**{**{k: _flat(v) for k, v in it.items() if isinstance(v, (str, int))}, "id": jid})
            jobs.append(RawJob(source=self.name, external_id=jid, title=title, url=link, location=loc or "United Arab Emirates",
                               description=desc, department=_flat(_path(it, f.get("department"))) or None,
                               posted_at=iso(parse_dt(_flat(_path(it, f.get("posted"))))) if f.get("posted") else None))
        complete = total is None or len(rows) >= int(total)
        return AdapterResult(jobs, complete=complete)


# ------------------------------------------------------------------------------------------------ Comeet
@register("comeet")
class Comeet(Adapter):
    """Comeet / Spark Hire hosted pages embed `COMPANY_POSITIONS_DATA = [...]`. config: {"company": "cbd", "uid": "14.007"}"""
    html_scraper = True

    def fetch(self, company, ctx):
        slug, uid = self.cfg(company, "company"), self.cfg(company, "uid")
        page = self.http.get(f"https://www.comeet.com/jobs/{slug}/{uid}", check_robots=True).text
        i = page.find("COMPANY_POSITIONS_DATA =")
        data = json_after(page, "COMPANY_POSITIONS_DATA", i) if i >= 0 else None
        if not isinstance(data, list):
            return AdapterResult([], complete=False, notes="positions data not found on the Comeet page")
        jobs = []
        for p in data:
            loc_d = p.get("location") or {}
            loc = ", ".join(x for x in (loc_d.get("city"), loc_d.get("country")) if x) or loc_d.get("name") or ""
            if not self.is_uae(loc, loc_d.get("country")):
                continue
            details = (p.get("custom_fields") or {}).get("details") or []
            desc = "\n\n".join(f"{d.get('name')}\n{html_to_text(d.get('value'))}" for d in details if d.get("value"))
            jobs.append(RawJob(source=self.name, external_id=str(p.get("uid")), title=p.get("name") or "",
                               url=p.get("url_comeet_hosted_page") or p.get("url_active_page") or "",
                               location=loc, description=desc, department=p.get("department"),
                               employment_type=p.get("employment_type"), country_code=loc_d.get("country"),
                               posted_at=iso(parse_dt(p.get("time_updated")))))
        return AdapterResult(jobs, complete=True)


# ------------------------------------------------------------------------------------------------ WhiteCarrot
@register("whitecarrot")
class WhiteCarrot(Adapter):
    """WhiteCarrot career sites (<company>.whitecarrot.ai) server-render their job list as escaped JSON."""
    html_scraper = True

    def fetch(self, company, ctx):
        base = self.cfg(company, "base").rstrip("/")
        page = self.http.get(base + "/", check_robots=True).text
        text = page.replace('\\"', '"').replace("\\\\", "\\")
        jobs, seen, details = [], set(), 0
        for m in re.finditer(r'\{\s*"id"\s*:\s*"([0-9a-f-]{36})"\s*,\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"location"\s*:', text):
            jid, title = m.group(1), json.loads(f'"{m.group(2)}"').strip()
            if jid in seen:
                continue
            seen.add(jid)
            loc_obj = json_after(text, "location", m.start()) or {}
            work = re.search(r'"workPlace"\s*:\s*"(\w+)"', text[m.end(): m.end() + 1500])
            loc = ", ".join(x for x in (loc_obj.get("city"), loc_obj.get("state"), loc_obj.get("country")) if x) \
                if isinstance(loc_obj, dict) else ""
            if work and work.group(1) == "REMOTE":
                loc = f"{loc} (Remote)" if loc else "Remote"
            if not self.is_uae(loc, (loc_obj or {}).get("countryCode") if isinstance(loc_obj, dict) else None):
                continue
            url = f"{base}/jobs/{jid}"
            desc = None
            if self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches:
                details += 1
                desc = extract_detail(self.http.get(url, check_robots=True).text)["description"] or ""
            jobs.append(RawJob(source=self.name, external_id=jid, title=title, url=url, location=loc, description=desc))
        return AdapterResult(jobs, complete=bool(seen), notes="" if seen else "no jobs found in page data")


# ------------------------------------------------------------------------------------------------ careers-page watch
ROLE_RX = re.compile(r"\b(analyst|manager|engineer|officer|specialist|associate|intern(ship)?|executive|lead|director|consultant|"
                     r"coordinator|developer|scientist|accountant|lecturer|professor|assistant|administrator|advisor|adviser|"
                     r"head of|supervisor|controller|architect|designer|representative|graduate|trainee|researcher)\b", re.I)


@register("page_watch")
class PageWatch(Adapter):
    """For employers that publish vacancies only as text on a careers page (no job list / feed).

    Each run reads the page (robots-checked) and extracts lines that look like job titles (headings, list items,
    links) inside the configured section. A new line becomes a vacancy lead pointing at the official page.
    config: {"url": ..., "section_start": regex?, "section_end": regex?, "min_len": 8}
    """
    html_scraper = True

    def fetch(self, company, ctx):
        urls = self.cfg(company, "urls", None, required=False) or [self.cfg(company, "url")]
        s_rx, e_rx = self.cfg(company, "section_start", None, required=False), self.cfg(company, "section_end", None, required=False)
        state = dict(ctx.state or {})
        jobs, changed_any, found_any = [], False, False
        for url in urls:
            page = self.http.get(url, check_robots=True).text
            body = SCRIPT_RX.sub(" ", page)
            body = re.sub(r"<(header|nav|footer)\b.*?</\1>", " ", body, flags=re.S | re.I)
            if s_rx:
                m = re.search(s_rx, body, re.I | re.S)
                body = body[m.start():] if m else body
            if e_rx:
                m = re.search(e_rx, body[100:], re.I | re.S)
                body = body[: m.start() + 100] if m else body
            cands = []
            for tag, inner in re.findall(r"<(h[1-5]|li|strong|b|a|p)\b[^>]*>(.*?)</\1>", body, re.S | re.I):
                t = norm_space(html_to_text(inner))
                if 6 <= len(t) <= 160 and ROLE_RX.search(t) and \
                        not re.search(r"cookie|privacy|subscribe|login|sign in|apply now$", t, re.I):
                    cands.append(t)
            text_now = norm_space(html_to_text(body))
            h = sha(text_now[:20000])
            changed_any = changed_any or state.get(url) not in (None, h)
            state[url] = h
            for t in dict.fromkeys(cands):
                parts = re.split(r"\s+(?:closing date|deadline|posted)\s*:?\s*", t, maxsplit=1, flags=re.I)
                title, closing = parts[0].strip(" -–|"), (parts[1] if len(parts) > 1 else None)
                if closing and is_closed(closing):
                    continue
                found_any = True
                jid = "pw-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
                i = text_now.find(t)
                jobs.append(RawJob(source=self.name, external_id=jid, title=title, url=url, location="United Arab Emirates",
                                   description=text_now[i: i + 1500] if i >= 0 else "",
                                   extra={"via": "careers page watch", "closing": closing}))
        notes = "careers page changed – no vacancy titles recognised" if changed_any and not found_any else ""
        return AdapterResult(jobs, complete=True, state_out=state, notes=notes)

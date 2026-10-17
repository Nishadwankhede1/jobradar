"""Workday Recruiting external career sites (the JSON 'cxs' endpoints behind *.myworkdayjobs.com)."""
from __future__ import annotations

import re

from ..http import HttpError
from ..location import region_query, region_rx
from ..util import html_to_text, iso, parse_dt
from .base import Adapter, AdapterResult, FetchContext, RawJob, register

PAGE = 20


def find_uae_facets(facets) -> tuple[str | None, list[str], bool]:
    """Walk Workday's (variously nested) facet tree. Returns (facetParameter, [ids], saw_location_facets)."""
    found: dict[str, list[tuple[str, int]]] = {}
    saw_loc = False

    def walk(nodes, param=None):
        nonlocal saw_loc
        for n in nodes or []:
            p = n.get("facetParameter") or param
            if p and re.search(r"loc|country|region", p, re.I):
                saw_loc = True
            vals = n.get("values")
            if vals and isinstance(vals, list) and vals and isinstance(vals[0], dict) and "values" in vals[0]:
                walk(vals, p)
            elif vals:
                for v in vals:
                    if isinstance(v, dict) and v.get("values"):
                        walk([v], p)
                    elif isinstance(v, dict) and v.get("id") and region_rx().search(v.get("descriptor", "")) and p \
                            and re.search(r"loc|country|region", p, re.I):
                        found.setdefault(p, []).append((v["id"], int(v.get("count") or 0)))

    walk(facets)
    if not found:
        return None, [], saw_loc
    country = [p for p in found if re.search(r"country", p, re.I)]
    best = country[0] if country else max(found, key=lambda k: sum(c for _, c in found[k]))
    return best, [i for i, _ in found[best]], saw_loc


class WorkdayBusy(HttpError):
    """Workday answered with an HTML page instead of JSON – it is limiting how often we ask (or is in maintenance)."""


@register("workday")
class Workday(Adapter):
    def _json(self, method: str, url: str, **kw):
        r = self.http.request(method, url, **kw)
        try:
            return r.json()
        except ValueError:
            raise WorkdayBusy(429, url, "Workday sent a web page instead of data (busy / limiting requests) – retried later")

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        host, tenant, site = self.cfg(company, "host"), self.cfg(company, "tenant"), self.cfg(company, "site")
        api = f"https://{host}/wday/cxs/{tenant}/{site}"
        hdr = {"Accept": "application/json", "Content-Type": "application/json"}
        first = self._json("POST", f"{api}/jobs", json_body={"appliedFacets": {}, "limit": PAGE, "offset": 0, "searchText": ""}, headers=hdr)
        param, ids, saw_loc = find_uae_facets(first.get("facets"))
        if param:
            applied, search, filtered = {param: ids}, "", True
        elif saw_loc:
            return AdapterResult([], complete=True, notes="no location facet for the watched countries → 0 jobs")
        else:
            applied, search, filtered = {}, region_query(), False

        postings, offset, total = [], 0, None
        for _ in range(ctx.max_pages):
            data = self._json("POST", f"{api}/jobs", json_body={"appliedFacets": applied, "limit": PAGE, "offset": offset,
                                                                "searchText": search}, headers=hdr)
            if total is None:
                total = data.get("total", 0)
            page = data.get("jobPostings", []) or []
            postings.extend(page)
            offset += len(page)
            if not page or offset >= (total or 0):
                break

        jobs, details = [], 0
        for p in postings:
            path = p.get("externalPath") or ""
            if not path:
                continue
            loc_text = p.get("locationsText") or ""
            ext_id = path.rsplit("_", 1)[-1] if "_" in path else path  # stable across runs
            if not filtered and not region_rx().search(loc_text) and not re.search(r"\d+ locations", loc_text, re.I):
                continue
            desc, posted, url, emp, loc = None, None, f"https://{host}/en-US/{site}{path}", None, loc_text
            if self.need_detail(ctx, ext_id, None) and details < ctx.max_detail_fetches:
                d = self._json("GET", f"{api}{path}", headers={"Accept": "application/json"})
                details += 1
                info = d.get("jobPostingInfo") or {}
                locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
                loc = " | ".join(x for x in locs if x) or loc_text
                if not filtered and not region_rx().search(loc + " " + ((info.get("country") or {}).get("descriptor") or "")):
                    continue
                desc = html_to_text(info.get("jobDescription"))
                posted = iso(parse_dt(info.get("startDate"))) or iso(parse_dt(info.get("postedOn")))
                url = info.get("externalUrl") or url
                emp = info.get("timeType")
            else:
                posted = iso(parse_dt(p.get("postedOn")))
            jobs.append(RawJob(source=self.name, external_id=ext_id, title=p.get("title", ""), url=url, location=loc,
                               description=desc, employment_type=emp, posted_at=posted))
        return AdapterResult(jobs, complete=filtered and offset >= (total or 0))

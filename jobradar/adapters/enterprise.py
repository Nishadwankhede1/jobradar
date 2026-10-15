"""Enterprise ATS adapters: Oracle Recruiting Cloud (HCM) and Eightfold."""
from __future__ import annotations

import urllib.parse

from ..http import HttpError
from ..location import UAE_RX
from ..util import html_to_text, iso, parse_dt
from .base import Adapter, AdapterResult, FetchContext, RawJob, register


# --------------------------------------------------------------------------- Oracle HCM
@register("oracle_hcm")
class OracleHCM(Adapter):
    """Oracle Recruiting Cloud 'Candidate Experience' REST endpoints (public, unauthenticated)."""
    PAGE = 25

    def _list_url(self, host, site, offset, location="United Arab Emirates"):
        # NOTE: locationsFacet only lists the top ~10 locations, so big employers (Marriott, Hilton) would show no
        # UAE facet. The text `location=` finder filter works on every tenant; results are re-filtered client-side.
        finder = (f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit={self.PAGE},offset={offset},"
                  f"sortBy=POSTING_DATES_DESC,location={location}")
        q = "onlyData=true&expand=requisitionList.secondaryLocations&finder=" + urllib.parse.quote(finder, safe="")
        return f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?{q}"

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        host, site = self.cfg(company, "host"), self.cfg(company, "site_number")
        location = self.cfg(company, "location_query", "United Arab Emirates", required=False)
        reqs, offset, total = [], 0, None
        for _ in range(ctx.max_pages):
            item = (self.http.get_json(self._list_url(host, site, offset, location)).get("items") or [{}])[0]
            if total is None:
                total = item.get("TotalJobsCount", 0)
            page = item.get("requisitionList") or []
            reqs.extend(page)
            offset += len(page)
            if not page or offset >= (total or 0):
                break
        loc_ids = None
        jobs, details = [], 0
        for r in reqs:
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in r.get("secondaryLocations") or []]
            loc = " | ".join(x for x in locs if x)
            cc = r.get("PrimaryLocationCountry")
            if not self.is_uae(loc, cc):  # text filter also returns e.g. Bahrain/Qatar/Nigeria
                continue
            rid = str(r["Id"])
            desc, emp = None, r.get("JobSchedule")
            if self.need_detail(ctx, rid, None) and details < ctx.max_detail_fetches:
                finder = urllib.parse.quote(f'ById;Id="{rid}",siteNumber={site}', safe="")
                d = (self.http.get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                                        f"?expand=all&onlyData=true&finder={finder}").get("items") or [{}])[0]
                details += 1
                desc = "\n\n".join(html_to_text(d.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                                                   "ExternalQualificationsStr") if d.get(k)) or r.get("ShortDescriptionStr")
                emp = d.get("JobSchedule") or d.get("WorkerType") or emp
            jobs.append(RawJob(source=self.name, external_id=rid, title=r.get("Title", ""),
                               url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}", location=loc,
                               description=desc, employment_type=emp, posted_at=iso(parse_dt(r.get("PostedDate"))),
                               country_code=cc))
        return AdapterResult(jobs, complete=total is not None and offset >= (total or 0))


# --------------------------------------------------------------------------- Eightfold
@register("eightfold")
class Eightfold(Adapter):
    """Eightfold Talent Intelligence career sites (v2 'apply' API, with the newer 'pcsx' API as fallback)."""

    def _v2(self, host, domain, loc, ctx):
        out, start, count = [], 0, None
        for _ in range(ctx.max_pages):
            d = self.http.get_json(f"https://{host}/api/apply/v2/jobs", params={"domain": domain, "location": loc, "start": start, "num": 10})
            count = d.get("count", 0) if count is None else count
            page = d.get("positions") or []
            out += [{"id": p.get("id"), "name": p.get("name"), "locations": p.get("locations") or [p.get("location", "")],
                     "ts": p.get("t_create"), "url": p.get("canonicalPositionUrl"), "desc": p.get("job_description"),
                     "dept": p.get("department"), "type": p.get("type")} for p in page]
            start += len(page)
            if not page or start >= count:
                break
        return out, start >= (count or 0)

    def _pcsx(self, host, domain, loc, ctx):
        out, start, count = [], 0, None
        for _ in range(ctx.max_pages):
            d = (self.http.get_json(f"https://{host}/api/pcsx/search", params={"domain": domain, "query": "", "location": loc, "start": start, "sort_by": "relevance"}).get("data") or {})
            count = d.get("count", 0) if count is None else count
            page = d.get("positions") or []
            out += [{"id": p.get("id") or p.get("positionId"), "name": p.get("name") or p.get("title"),
                     "locations": p.get("locations") or p.get("standardizedLocations") or [p.get("location") or ""],
                     "ts": p.get("postedTs") or p.get("t_create"), "url": p.get("positionUrl") or p.get("canonicalPositionUrl"),
                     "desc": None, "dept": p.get("department"), "type": p.get("workLocationOption"), "pcsx": True} for p in page]
            start += len(page)
            if not page or start >= count:
                break
        return out, start >= (count or 0)

    def fetch(self, company, ctx):
        host, domain = self.cfg(company, "host"), self.cfg(company, "domain")
        loc_q = self.cfg(company, "location_query", "United Arab Emirates", required=False)
        api = self.cfg(company, "api", "auto", required=False)
        try:
            positions, complete = (self._pcsx if api == "pcsx" else self._v2)(host, domain, loc_q, ctx)
        except HttpError:
            if api != "auto":
                raise
            positions, complete = self._pcsx(host, domain, loc_q, ctx)
        jobs, details = [], 0
        for p in positions:
            loc = " | ".join(x for x in p["locations"] if x)
            if not self.is_uae(loc):
                continue
            pid = str(p["id"])
            desc = html_to_text(p["desc"]) if p["desc"] else None
            if desc is None and self.need_detail(ctx, pid, None) and details < ctx.max_detail_fetches:
                details += 1
                if p.get("pcsx"):
                    d = self.http.get_json(f"https://{host}/api/pcsx/position_details", params={"position_id": pid, "domain": domain, "hl": "en"}).get("data") or {}
                    desc = html_to_text(d.get("jobDescription"))
                else:
                    d = self.http.get_json(f"https://{host}/api/apply/v2/jobs/{pid}", params={"domain": domain})
                    desc = html_to_text(d.get("job_description"))
            url = p["url"] or f"https://{host}/careers/job/{pid}"
            if url.startswith("/"):
                url = f"https://{host}{url}"
            jobs.append(RawJob(source=self.name, external_id=pid, title=p["name"] or "", url=url, location=loc,
                               description=desc, department=p.get("dept"), posted_at=iso(parse_dt(p.get("ts")))))
        return AdapterResult(jobs, complete=complete)

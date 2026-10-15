"""Headless-browser connector for careers sites that only render jobs with JavaScript.

Used only where the site's robots.txt allows the pages we open; it never logs in, solves CAPTCHAs or works around
bot checks (a challenge page simply makes the source fail). Needs `pip install playwright && playwright install
chromium`; the GitHub workflow `browser-scan.yml` does that and runs these sources hourly.

config: {"urls": ["https://lifeattiktok.com/search?keyword=Dubai&offset={offset}"], "offsets": [0, 12, 24],
         "link_pattern": "/search/\\d+$", "wait_ms": 6000, "detail": true}
"""
from __future__ import annotations

import os
import re
import urllib.parse

from ..location import UAE_RX
from ..util import norm_space
from .base import Adapter, AdapterResult, FetchContext, RawJob, register

CARD_JS = """(pat) => { const rx = new RegExp(pat); const out = [];
  for (const a of document.querySelectorAll('a[href]')) { if (!rx.test(a.href)) continue;
    let el = a, txt = a.innerText || '';
    for (let i = 0; i < 5 && el.parentElement; i++) {   // grow to the job card, never to a list of several jobs
      const up = el.parentElement; const n = [...up.querySelectorAll('a[href]')].filter(x => rx.test(x.href) && x.href !== a.href).length;
      if (n) break; el = up; txt = el.innerText || ''; }
    out.push({href: a.href, title: (a.querySelector('h1,h2,h3,h4,[class*=title]')||a).innerText || a.innerText, card: txt.slice(0, 600)}); }
  return out; }"""


class BrowserUnavailable(RuntimeError):
    pass


def browser_enabled() -> bool:
    return os.environ.get("JOBRADAR_BROWSER", "").lower() in ("1", "true", "yes")


@register("browser")
class Browser(Adapter):
    html_scraper = True

    def fetch(self, company, ctx: FetchContext) -> AdapterResult:
        try:
            from playwright.sync_api import sync_playwright  # optional dependency
        except ImportError as e:  # pragma: no cover - depends on runner
            raise BrowserUnavailable("playwright is not installed (runs in the hourly browser workflow)") from e
        urls = self.cfg(company, "urls")
        urls = [urls] if isinstance(urls, str) else urls
        offsets = self.cfg(company, "offsets", None, required=False) or [None]
        pattern = self.cfg(company, "link_pattern")
        wait = int(self.cfg(company, "wait_ms", 6000, required=False))
        want_detail = self.cfg(company, "detail", True, required=False)
        pages = [u.format(offset=o) if o is not None else u for u in urls for o in offsets]
        for u in pages:
            if not self.http.allowed_by_robots(u):
                raise PermissionError(f"robots.txt disallows {u}")
        rows: dict[str, dict] = {}
        jobs, details = [], 0
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page(user_agent=self.http.user_agent)
            for u in pages:
                pg.goto(u, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(wait)
                found = pg.evaluate(CARD_JS, pattern)
                for c in found:
                    href = c["href"].split("#")[0]
                    rows.setdefault(href, c)
                if not found:
                    break
            for href, c in rows.items():
                card = norm_space(c.get("card"))
                title = norm_space((c.get("title") or "").strip().split("\n")[0])
                if not UAE_RX.search(card + " " + title):
                    continue
                jid = (re.search(r"(\d{6,})", href) or [None, href])[1]
                desc = None
                if want_detail and self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches \
                        and self.http.allowed_by_robots(href):
                    details += 1
                    pg.goto(href, wait_until="domcontentloaded", timeout=45000)
                    pg.wait_for_timeout(min(wait, 4000))
                    desc = norm_space(pg.evaluate("() => (document.querySelector('main')||document.body).innerText"))[:20000]
                loc = next((m.group(0) for m in UAE_RX.finditer(card)), "United Arab Emirates")
                jobs.append(RawJob(source=self.name, external_id=jid, title=title[:200], url=href, location=loc,
                                   description=desc if want_detail else card))
            b.close()
        return AdapterResult(jobs, complete=bool(rows), notes="" if rows else "page rendered no job links")

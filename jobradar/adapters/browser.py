"""Headless-browser connector for careers sites that only render jobs with JavaScript.

Used only where the site's robots.txt allows the pages we open; it never logs in, solves CAPTCHAs or works around
bot checks (a challenge page simply makes the source fail). Needs `pip install playwright` plus a browser:
  engine "lightpanda" – Lightpanda (a light headless browser, ~2-3 s a page), driven over CDP; its binary is on PATH
                        or in $LIGHTPANDA_BIN, and it obeys robots.txt itself as well
  engine "chrome"     – the Google Chrome already installed on the machine (GitHub's runners have one)
  engine "chromium"   – Playwright's own Chromium (`playwright install chromium`), the default

config: {"urls": ["https://lifeattiktok.com/search?keyword=Dubai&offset={offset}"], "offsets": [0, 12, 24],
         "link_pattern": "/search/\\d+$", "wait_ms": 6000, "detail": true, "engine": "lightpanda",
         "wait_until": "domcontentloaded" | "networkidle", "all_uae": false}
all_uae: the page lists only UAE jobs (e.g. its links contain /uae/), so a card needn't name the city to count.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
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


@contextlib.contextmanager
def lightpanda_cdp():
    """A Lightpanda CDP server on a free local port for the duration of one source; yields its endpoint."""
    exe = os.environ.get("LIGHTPANDA_BIN") or shutil.which("lightpanda")
    if not exe:
        raise BrowserUnavailable("Lightpanda is not installed (set LIGHTPANDA_BIN or put `lightpanda` on PATH)")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    proc = subprocess.Popen([exe, "serve", "--obey-robots", "--host", "127.0.0.1", "--port", str(port), "--log-level", "err"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t = time.monotonic()
        while time.monotonic() - t < 15:
            try:
                socket.create_connection(("127.0.0.1", port), 1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise BrowserUnavailable("Lightpanda didn't start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
        engine = self.cfg(company, "engine", "chromium", required=False)
        until = self.cfg(company, "wait_until", "domcontentloaded", required=False)
        all_uae = bool(self.cfg(company, "all_uae", False, required=False))
        pages = [u.format(offset=o) if o is not None else u for u in urls for o in offsets]
        for u in pages:
            if not self.http.allowed_by_robots(u):
                raise PermissionError(f"robots.txt disallows {u}")
        rows: dict[str, dict] = {}
        jobs, details = [], 0
        with contextlib.ExitStack() as stack:
            p = stack.enter_context(sync_playwright())
            if engine == "lightpanda":
                b = p.chromium.connect_over_cdp(stack.enter_context(lightpanda_cdp()))
                pg = (b.contexts[0] if b.contexts else b.new_context()).new_page()
            else:
                b = p.chromium.launch(headless=True, channel="chrome" if engine == "chrome" else None)
                pg = b.new_page(user_agent=self.http.user_agent)
            for u in pages:
                pg.goto(u, wait_until=until, timeout=45000)
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
                if not (all_uae or UAE_RX.search(card + " " + title)):
                    continue
                jid = (re.search(r"(\d{6,})", href) or [None, href])[1]
                desc = None
                if want_detail and self.need_detail(ctx, jid, None) and details < ctx.max_detail_fetches \
                        and self.http.allowed_by_robots(href):
                    details += 1
                    pg.goto(href, wait_until=until, timeout=45000)
                    pg.wait_for_timeout(min(wait, 4000))
                    desc = norm_space(pg.evaluate("() => (document.querySelector('main')||document.body).innerText"))[:20000]
                loc = next((m.group(0) for m in UAE_RX.finditer(card + " " + (desc or ""))), "United Arab Emirates")
                jobs.append(RawJob(source=self.name, external_id=jid, title=title[:200], url=href, location=loc,
                                   description=desc if want_detail else card))
            b.close()
        return AdapterResult(jobs, complete=bool(rows), notes="" if rows else "page rendered no job links")

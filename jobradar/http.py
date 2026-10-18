"""Polite HTTP client (stdlib only): retries + backoff, per-host throttling, conditional requests, robots.txt."""
from __future__ import annotations

import gzip
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field

from .util import log, redact


@dataclass
class Response:
    status: int
    url: str
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return self.body.decode(enc, "replace")

    def json(self):
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpError(Exception):
    def __init__(self, status: int, url: str, msg: str = ""):
        super().__init__(redact(f"HTTP {status} for {url} {msg}".strip()))
        self.status, self.url = status, url


class NotModified(Exception):
    """Raised by adapters when a conditional request returns 304 – nothing changed at the source."""


class Transport:
    """Real network transport using urllib (honours HTTPS_PROXY env)."""

    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout

    _fallback_ctx: ssl.SSLContext | None = None

    @classmethod
    def _verified_fallback(cls) -> ssl.SSLContext | None:
        """Some sites send an incomplete certificate chain; Windows Python then can't verify them. Verification is never
        switched off – we retry with the OS trust store (truststore, which completes chains) or the certifi bundle."""
        if cls._fallback_ctx is None:
            try:
                import truststore  # type: ignore
                cls._fallback_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                cls._fallback_ctx = cls._windows_store_ctx() or False  # type: ignore[assignment]
                if not cls._fallback_ctx:
                    try:
                        import certifi  # type: ignore
                        cls._fallback_ctx = ssl.create_default_context(cafile=certifi.where())
                    except Exception:
                        cls._fallback_ctx = False  # type: ignore[assignment]
        return cls._fallback_ctx or None

    @staticmethod
    def _windows_store_ctx() -> ssl.SSLContext | None:
        """Every root/intermediate in the Windows certificate stores (incl. a company or antivirus root that inspects
        HTTPS) – Anaconda's Python sometimes uses only its own bundle."""
        if not hasattr(ssl, "enum_certificates"):
            return None
        ctx = ssl.create_default_context()
        added = 0
        for store in ("ROOT", "CA"):
            try:
                for cert, enc, trust in ssl.enum_certificates(store):
                    if enc == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in (trust or ())):
                        try:
                            ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
                            added += 1
                        except ssl.SSLError:
                            pass
            except Exception:
                pass
        return ctx if added else None

    def send(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            try:
                r_cm = urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.URLError as e:
                ctx = self._verified_fallback() if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError) else None
                if not ctx:
                    raise
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                r_cm = urllib.request.urlopen(req, timeout=self.timeout, context=ctx)
            with r_cm as r:
                raw = r.read()
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            status = e.code
        enc = hdrs.get("content-encoding", "")
        try:
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw)
        except Exception:
            pass
        return Response(status, url, hdrs, raw)


class HttpClient:
    def __init__(self, transport: Transport | None = None, user_agent: str | None = None,
                 min_interval: float = 0.6, max_retries: int = 3, cache: dict | None = None,
                 respect_robots: bool = True):
        self.transport = transport or Transport()
        self.user_agent = user_agent or "JobRadar/1.0 (personal job-alert monitor; low-frequency polling)"
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.cache = cache if cache is not None else {}  # url -> {"etag":..., "last_modified":...}
        self.respect_robots = respect_robots
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._glock = threading.Lock()
        self.request_count = 0
        self.sleep = time.sleep
        self.deadline: float | None = None      # time.monotonic() after which requests fail fast
        self._local = threading.local()          # current source id (set per worker thread)
        self.pending: dict[str, dict] = {}        # source id -> {url: validators}, applied only on success

    def set_source(self, source_id: str | None):
        self._local.source = source_id

    def take_pending(self, source_id: str) -> dict:
        with self._glock:
            return self.pending.pop(source_id, {})

    # -------------------------------------------------------- throttling
    # Shared ATS servers rate-limit per caller, not per employer: every *.wd3.myworkdayjobs.com tenant (or Oracle HCM pod)
    # is one server, so they share one pace. (host pattern → (group key, seconds between requests))
    SHARED_HOSTS = [(re.compile(r"(?:^|\.)(wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)$"), 1.5),
                    (re.compile(r"(?:^|\.)(fa\.\w+\.oraclecloud\.com)$"), 1.2),
                    (re.compile(r"^(apply\.workable\.com)$"), 2.0)]   # all Workable employers share one server

    def _pace(self, host: str) -> tuple[str, float]:
        for rx, gap in self.SHARED_HOSTS:
            m = rx.search(host)
            if m:
                return m.group(1), max(gap, self.min_interval)
        return host, self.min_interval

    def _throttle(self, host: str):
        host, gap = self._pace(host)
        with self._glock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            wait = gap - (time.monotonic() - self._host_last.get(host, 0))
            if wait > 0:
                self.sleep(wait)
            self._host_last[host] = time.monotonic()

    # -------------------------------------------------------- robots
    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.transport.send("GET", base + "/robots.txt", {"User-Agent": self.user_agent}, None)
                if r.status >= 400:
                    rp = None  # no robots file → allowed
                else:
                    rp.parse(r.text.splitlines())
            except Exception:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # -------------------------------------------------------- request
    def request(self, method: str, url: str, *, params: dict | None = None, json_body=None, data: bytes | None = None,
                headers: dict | None = None, conditional: bool = False, check_robots: bool = False) -> Response:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        if not url.isascii():  # e.g. an en dash in a job URL → percent-encode it (urllib only sends ASCII)
            url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
        if check_robots and not self.allowed_by_robots(url):
            raise HttpError(999, url, "disallowed by robots.txt")
        h = {"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
             "Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US,en;q=0.9"}
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        cached = self.cache.get(url) if conditional else None
        if cached:
            if cached.get("etag"):
                h["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                h["If-Modified-Since"] = cached["last_modified"]
        host = urllib.parse.urlsplit(url).netloc
        last_exc: Exception | None = None
        retries = self.max_retries
        for attempt in range(retries + 1):
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise HttpError(0, url, "run time budget exhausted – deferred to next run")
            self._throttle(host)
            try:
                self.request_count += 1
                r = self.transport.send(method, url, h, body)
            except Exception as e:  # network/timeouts
                last_exc = e
                if attempt < retries and (method == "GET" or "/wday/cxs/" in url):  # never blindly re-send a non-idempotent POST
                    self.sleep(min(30, (2 ** attempt) + random.random()))
                    continue
                raise HttpError(0, url, f"network error: {type(e).__name__}: {e}") from e
            if r.status == 304:
                raise NotModified(url)
            retry_codes = (429, 500, 502, 503, 504) if method == "GET" or "/wday/cxs/" in url else (429,)
            if r.status in retry_codes and attempt < retries:
                ra = r.headers.get("retry-after", "")
                delay = float(ra) if ra.isdigit() else (2 ** attempt) * 1.5 + random.random()
                log.debug("retry %s after %.1fs (status %s)", url, delay, r.status)
                self.sleep(min(delay, 60))
                continue
            if r.status >= 400:
                raise HttpError(r.status, url)
            if conditional and (r.headers.get("etag") or r.headers.get("last-modified")):
                v = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}
                src = getattr(self._local, "source", None)
                if src is None:
                    self.cache[url] = v
                else:  # only trusted once the whole source scan succeeded (see pipeline.handle)
                    with self._glock:
                        self.pending.setdefault(src, {})[url] = v
            return r
        raise HttpError(0, url, str(last_exc))

    def get(self, url, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url, **kw) -> Response:
        return self.request("POST", url, **kw)

    def get_json(self, url, **kw):
        return self.get(url, **kw).json()

    def post_json(self, url, **kw):
        return self.post(url, **kw).json()

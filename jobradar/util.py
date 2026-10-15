"""Shared helpers: time, text normalisation, hashing, logging with secret redaction."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

# ---------------------------------------------------------------- time

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def now_iso() -> str:
    return iso(utcnow())


def parse_dt(value) -> datetime | None:
    """Parse the many date formats ATS feeds use. Returns aware UTC datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # epoch ms
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return parse_dt(int(s))
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d.%m.%Y", "%A, %B %d, %Y", "%A, %d %B %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.search(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", s, re.I)
    if m:
        days = 0 if m.group(1).lower() == "today" else 1 if m.group(1).lower() == "yesterday" else int(m.group(2))
        return (utcnow() - timedelta(days=days)).replace(hour=0, minute=0, second=0)
    return None


# ---------------------------------------------------------------- text

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Convert (possibly entity-escaped) HTML into readable plain text."""
    if not value:
        return ""
    s = str(value)
    if "&lt;" in s and "<" not in s:  # Greenhouse returns escaped HTML
        s = html.unescape(s)
    if "<" not in s:
        return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()
    p = _TextExtractor()
    try:
        p.feed(s)
        p.close()
    except Exception:  # malformed html – fall back to tag strip
        return re.sub(r"<[^>]+>", " ", s)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def norm_space(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


_TITLE_NOISE = re.compile(
    r"\((?:m/f/d|f/m/d|m/w/d|all genders|hybrid|remote|on-?site|full[- ]time|part[- ]time|contract|temporary|"
    r"permanent|req\s*#?\s*\w+|job id[:\s]*\w+)\)|\b(?:req|job)\s*(?:id|#)\s*[:#]?\s*[\w-]+",
    re.I,
)


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"\s[-–|@,]\s*(dubai|uae|united arab emirates|abu dhabi|difc|mena|gcc)\b.*$", " ", t)
    t = re.sub(r"\b(sr|snr)\b\.?", "senior", t)
    t = re.sub(r"\b(jr)\b\.?", "junior", t)
    t = re.sub(r"\bbi\b", "business intelligence", t)
    t = re.sub(r"[^a-z0-9+#& ]+", " ", t)
    return norm_space(t)


def normalize_company(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch)).lower()  # "Müller`s" == "Muller's"
    n = re.sub(r"['`’]s\b", "s", n)
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"\b(llc|l\.l\.c|ltd|limited|inc|plc|fz-?llc|fze|dmcc|group|holding|holdings|pjsc|co|company|middle east|me|uae)\b\.?", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return norm_space(n)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def sha(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p or "").encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:20]


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+#.]*", (s or "").lower())


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- logging

_SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "WHATSAPP_TOKEN", "TWILIO_AUTH_TOKEN", "LLM_API_KEY", "ANTHROPIC_API_KEY",
               "GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "IMAP_PASSWORD", "JOOBLE_API_KEY")


def redact(text: str | None) -> str | None:
    """Remove credentials from any string before it is logged OR persisted (DB is committed to git)."""
    if not text:
        return text
    for k in _SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 6:
            text = text.replace(v, f"<{k}>")
    text = re.sub(r"bot\d{5,}:[A-Za-z0-9_-]{10,}", "bot<redacted>", text)
    text = re.sub(r"(jooble\.org/api/)[^/?\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)=([^&\s]+)", r"\1=<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    """Never let credentials reach logs, even if they appear inside URLs or exceptions."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        return True


def setup_logging(level: str = "INFO") -> logging.Logger:
    log = logging.getLogger("jobradar")
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S"))
        h.addFilter(RedactingFilter())
        log.addHandler(h)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    return log


log = logging.getLogger("jobradar")

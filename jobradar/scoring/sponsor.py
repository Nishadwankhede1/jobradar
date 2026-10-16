"""Visa-sponsor, language and relocation signals for the Europe instance (JOBRADAR_REGION=eu).

Three independent signals, shown as badges on the dashboard:
  register  – the employer is on an official sponsor register (NL: IND "recognised sponsors" for work, refreshed
              monthly by `python -m jobradar sponsors`; DE has no register; UK: Home Office list – later phase)
  ad        – the job ad itself offers visa sponsorship / relocation ("offers"), or rules it out ("refuses")
  english   – the ad is written in English and doesn't demand Dutch / German
Nothing here is guessed from the company's reputation: no evidence = "unknown", never "sponsors".
"""
from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path

from ..region import ROOT, overlay

REGISTER_FILE = {"nl": ROOT / "state" / "sponsors_nl.json"}
IND_WORK_URL = "https://ind.nl/en/public-register-recognised-sponsors/public-register-work"

SPONSOR_POS = re.compile(
    r"visa sponsorship (?:is )?(?:available|provided|offered|possible|support)|(?:we|company) (?:can|will|do|does|are able to) "
    r"(?:provide |offer )?(?:visa )?sponsor|sponsorship (?:is )?(?:available|provided|offered|possible)|(?:offer|provide|including|with) "
    r"(?:full )?(?:visa|work permit) (?:sponsorship|support|assistance)|highly[- ]skilled migrant|kennismigrant|"
    r"(?:eu )?blue card|30 ?% (?:ruling|tax ruling|tax benefit)|(?:visa|immigration) (?:support|assistance|process)|"
    r"open to (?:international|non-eu) (?:candidates|applicants)|(?:international|non-eu) (?:candidates|applicants) (?:are )?welcome|"
    r"we sponsor|sponsor(?:ing)? (?:your|the) (?:visa|work permit)", re.I)
SPONSOR_NEG = re.compile(
    r"\bno (?:visa )?sponsorship|(?:unable|not able|cannot|can't|can not|won't|will not|do not|don't|does not|doesn't|are not able) "
    r"(?:to )?(?:provide |offer |support |consider )?(?:visa |work permit )?sponsor|not (?:in a position|able) to (?:provide )?(?:visa )?sponsor|"
    r"without (?:the need for |requiring |needing )?(?:visa |employer )?sponsorship|must (?:already )?(?:have|hold|possess) (?:a |the |an )?"
    r"(?:valid |existing |current )?(?:eu |dutch |uk |german )?(?:right|permit|authori[sz]ation|work permit|residence permit) to work|"
    r"(?:eu|eea) (?:citizens?|passport holders?|nationals?) only|only (?:eu|eea) (?:citizens|nationals|candidates)|"
    r"(?:valid |existing )?(?:eu |dutch |uk )?(?:work permit|right to work)[^.\n]{0,40}(?:is )?(?:required|mandatory|a must|essential)|"
    r"we (?:are )?(?:unable|not able) to (?:offer|provide) relocation", re.I)
RELOCATION = re.compile(r"relocation (?:package|support|assistance|bonus|budget|allowance|help|costs?)|(?:help|support|assist)\w* "
                        r"(?:you )?(?:with )?(?:your )?relocat|relocate you|we (?:cover|pay) (?:your )?(?:relocation|moving|flight)", re.I)

EN = {"the", "and", "you", "with", "for", "our", "will", "are", "your", "we", "to", "of", "in", "is", "as", "be", "on", "team"}
NL = {"de", "het", "een", "en", "van", "je", "jij", "wij", "voor", "met", "zijn", "ons", "onze", "bent", "heb", "naar", "bij",
      "ook", "wat", "dat", "niet", "kun", "jouw", "wordt", "werken", "ervaring"}
DE = {"der", "die", "das", "und", "mit", "für", "wir", "sie", "ein", "eine", "ist", "du", "bei", "zu", "auf", "dich", "dein",
      "deine", "unser", "unsere", "werden", "oder", "nicht", "erfahrung", "kenntnisse"}
LANG_NAME = {"nl": "Dutch", "de": "German"}
GENERIC_TAIL = {"com", "technologies", "technology", "tech", "software", "payments", "bank", "labs", "digital", "solutions",
                "systems", "trading", "financial", "finance", "fintech", "platform", "online", "operations", "development",
                "hq", "amsterdam", "nl", "bv", "nv", "and", "company"}

MONEY = re.compile(r"(€|eur\b|euro|£|gbp)\s?(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:[.,]\d+)?)\s?(k\b)?(?:\s*(?:-|–|to|tot|bis)\s*(?:€|eur|£)?\s?"
                   r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:[.,]\d+)?)\s?(k\b)?)?[^.\n]{0,25}?(per month|a month|/ ?month|monthly|per maand|"
                   r"p\.?m\.?|per year|a year|/ ?year|per annum|p\.?a\.?|annually|yearly|gross)?", re.I)


def ad_language(text: str) -> str | None:
    """'en' | 'nl' | 'de' from common function words; None when the text is too short to tell."""
    words = re.findall(r"[a-zäöüß]+", (text or "")[:6000].lower())
    if len(words) < 60:
        return None
    c = {k: sum(1 for w in words if w in s) for k, s in (("en", EN), ("nl", NL), ("de", DE))}
    best = max(c, key=c.get)
    return best if c[best] >= 8 else None


def norm_org(name: str) -> str:
    s = html.unescape(name or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(b ?v|n ?v|bv|nv|gmbh|ag|se|ltd|limited|plc|llc|inc|corp|corporation|co|company|holding|holdings|group|"
               r"nederland|netherlands|the|europe|emea|international|benelux|global|services|vof|cv|stichting)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=4)
def _register(country: str) -> dict:
    p = REGISTER_FILE.get(country)
    if not p or not p.exists():
        return {}
    try:
        names = json.loads(p.read_text(encoding="utf-8")).get("names") or []
    except (OSError, ValueError):
        return {}
    out = {}
    for n in names:
        k = norm_org(n)
        if k and k not in out:
            out[k] = n
    return out


def register_match(country: str, *company_names: str) -> str | None:
    """Official register entry for this employer (exact name after removing B.V./N.V./GmbH…, or same leading words)."""
    reg = _register(country)
    if not reg:
        return None
    for name in company_names:
        k = norm_org(name)
        if len(k) < 3:
            continue
        if k in reg:
            return reg[k]
        if len(k) >= 4:  # "Picnic" ↔ "Picnic Technologies B.V." – only generic extra words, never "Elastic" ↔ "Elastic Media"
            for rk, orig in reg.items():
                if rk.startswith(k + " ") and set(rk[len(k):].split()) <= GENERIC_TAIL:
                    return orig
    return None


def salary_monthly_eur(text: str) -> float | None:
    """Highest monthly gross figure the ad states (EUR or GBP), or None."""
    best = None
    for m in MONEY.finditer(text or ""):
        def num(v, k):
            if not v:
                return None
            v = re.sub(r"[.,\s](?=\d{3}\b)", "", v).replace(",", ".")
            try:
                x = float(v)
            except ValueError:
                return None
            return x * 1000 if k else x
        hi = num(m.group(4), m.group(5) or m.group(3)) or num(m.group(2), m.group(3))
        if not hi or hi < 1000:
            continue
        per = (m.group(6) or "").lower()
        monthly = hi / 12 if ("year" in per or "annum" in per or "p.a" in per or per.startswith("pa") or "annual" in per
                             or (not per and hi >= 20000)) else hi
        if m.group(1).lower() in ("£", "gbp"):
            monthly *= 1.17
        if 1500 <= monthly <= 40000:
            best = max(best or 0, monthly)
    return best


def visa_info(job: dict, company: dict, tier: str) -> dict:
    """The badges for one job."""
    text = job.get("description") or ""
    names = [job.get("company_display") or "", (company or {}).get("name") or ""]
    names += [re.sub(r"\s*\(.*$", "", n) for n in names]
    names += list(((company or {}).get("ats_config") or {}).get("sponsor_names") or [])  # legal name on the register
    reg = register_match("nl", *names) if tier in ("nl", "remote") else None
    lang = ad_language(text)
    neg, pos = SPONSOR_NEG.search(text), SPONSOR_POS.search(text)
    sal = salary_monthly_eur(text)
    o = overlay().get("region", {})
    floor = {"nl": float(o.get("nl_hsm_monthly_min", 4357)), "de": float(o.get("de_blue_card_yearly_min", 45934)) / 12,
             "uk": float(o.get("uk_skilled_worker_yearly_min", 41700)) * 1.17 / 12}.get(tier)
    return {"register": reg, "ad": "refuses" if neg else "offers" if pos else None,
            "ad_quote": (neg or pos).group(0)[:80] if (neg or pos) else None,
            "relocation": bool(RELOCATION.search(text)), "language": lang,
            "salary_month": round(sal) if sal else None,
            "salary_ok": None if not (sal and floor) else sal >= floor, "salary_floor": round(floor) if floor else None,
            "likely": bool(reg or (pos and not neg))}


def assess_eu(e, title: str, text: str, tier: str) -> None:
    """Hard rules for Europe: the employer must be able to hire someone who needs a visa, in English."""
    neg = SPONSOR_NEG.search(text)
    if neg:
        e.hard.append(f"Employer won't sponsor a visa (\"{neg.group(0)[:60]}\")")
    lang = ad_language(text)
    if lang in LANG_NAME:
        e.hard.append(f"Job ad is written in {LANG_NAME[lang]} – the team likely works in {LANG_NAME[lang]}")
    if SPONSOR_POS.search(text):
        e.notes.append("Ad mentions visa sponsorship / relocation support")
    elif not neg:
        e.uncertain.append("Ad doesn't mention visa sponsorship – check the sponsor badge / ask the recruiter")


def fetch_nl_register(http) -> dict:
    """Download the IND public register (work) – an HTML table of organisation + KvK number."""
    page = http.get_text(IND_WORK_URL) if hasattr(http, "get_text") else http.get(IND_WORK_URL).text
    cell = r"((?:(?!</?t[dhr]\b).)*?)"  # one table cell's content – never runs into the next cell / row
    rows = re.findall(rf"<tr[^>]*>\s*<t[dh][^>]*>{cell}</t[dh]>\s*<t[dh][^>]*>\s*(\d{{8}})\s*</t[dh]>", page, re.S | re.I)
    names = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", n))).strip(' "') for n, _ in rows]
    names = [n for n in names if n]
    upd = re.search(r"last updated on (\d{1,2} \w+ 20\d\d)", page, re.I)
    return {"source": IND_WORK_URL, "updated": upd.group(1) if upd else None, "count": len(names), "names": names}


def save_register(country: str, data: dict) -> Path:
    p = REGISTER_FILE[country]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _register.cache_clear()
    return p


def refresh_registers(http, db=None, max_age_days: float = 7) -> str:
    """Re-download the NL sponsor register when it is missing or older than a week (it changes monthly)."""
    import time
    p = REGISTER_FILE["nl"]
    if p.exists() and time.time() - p.stat().st_mtime < max_age_days * 86400:
        return "fresh"
    try:
        data = fetch_nl_register(http)
    except Exception as e:  # keep the old copy; never break a scan over this
        return f"failed: {type(e).__name__}"
    if data["count"] < 1000:  # page layout changed / blocked – don't replace a good list with a broken one
        return f"suspicious ({data['count']} rows) – kept the old list"
    save_register("nl", data)
    if db is not None:  # badges depend on the register → let the next scoring pass recheck plausible jobs
        db.execute("DELETE FROM kv WHERE key='scorer_version'")
        db.commit()
    return f"updated ({data['count']} sponsors, {data.get('updated')})"

"""Two-stage, explainable job scoring.

Stage A – fast deterministic gate: location, status, job family (title taxonomy + description semantics).
Stage B – weighted sub-scores (0–10 each):
    technical 30% · responsibilities 25% · experience 15% · eligibility 15% · industry 10% · career 5%
    final = 1 + 0.9 × weighted_sum  → 1..10
Hard eligibility restrictions are tracked separately: they force `eligible=False` and block notifications no
matter how high the weighted score is (an LLM can never override them).
"""
from __future__ import annotations

import math
import re
from collections import Counter

from ..location import classify
from ..util import normalize_title, tokens
from . import eligibility as elig
from .skills import HUMAN, RESP_NEG_RX, RESP_POS_RX, SKILLS, extract_skills, skill_counts

CORE_SIGNAL = {"power_bi", "dax", "sql", "dashboards", "kpi", "etl", "data_modeling", "tableau", "looker", "qlik",
               "data_visualization", "reporting_automation", "data_quality", "snowflake", "excel", "python", "data_warehouse",
               "microsoft_fabric", "azure_data_factory", "ssrs", "statistics", "forecasting"}

STOP = set("""a an the and or of to in for with on at by from as is are be been this that these those you your we our
our they their it its will would can could should may might must have has had do does did not no yes than then
so such into about over under within across per via etc including include includes role job position team work
working company candidate candidates ability strong excellent good experience years year skills skill knowledge
using use used new across all any each other more most well also plus who what which where when why how""".split())

INDUSTRY_SCORE = [
    (r"asset management|investment|fund|wealth|private equity|hedge|financial data|ratings|exchange|capital markets", 10),
    (r"bank|fintech|payment|insurance|reinsurance|crypto|finance|credit", 9),
    (r"consult|professional services|technology|software|saas|data|e-?commerce|marketplace|proptech|media|telecom", 8),
    (r"retail|fmcg|consumer|logistics|aviation|airline|real estate|property|healthcare|pharma|hospitality|energy|"
     r"utilities|government|automotive|mobility|travel|delivery|quick-commerce|food", 7),
    (r"education|university|school|non-profit", 6),
]

YEARS_RX = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)\b(?:'|’)?"
                      r"(?!\s*(?:ago|old|of age|warranty|contract|fixed|term|history))(?=[^.\n]{0,60}(?:experience|exp\b)|\s+(?:in|of|as|working|within|across|hands|post|relevant|progressive|professional))", re.I)
MIN_YEARS_RX = re.compile(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)

LEVELS = [("chief", r"\bchief\b|\bcxo\b|\bcdo\b"), ("vp", r"\bvp\b|vice president"), ("director", r"\bdirector\b"),
          ("head", r"\bhead of\b|\bhead\b"), ("manager", r"\bmanager\b(?! data)|\bmgr\b"), ("lead", r"\blead\b|\bprincipal\b|\bstaff\b"),
          ("senior", r"\bsenior\b|\bsr\b"), ("intern", r"\bintern(ship)?\b|\bco-?op\b|\bplacement\b"),
          ("graduate", r"\bgraduate\b|\btrainee\b|\bentry[- ]level\b|\bfresh(er)?\b"), ("junior", r"\bjunior\b|\bjr\b"),
          ("associate", r"\bassociate\b"), ("analyst", r"analyst|developer|engineer|specialist|executive|consultant|officer")]

SCORER_VERSION = "2026-10-16a"  # bump to force a one-off rescore of stored jobs after rule changes

# Titles that the taxonomy misses but that still look like analytics work (used to cap description-rescued roles)
TITLE_DATA_RX = re.compile(r"analy|\bdata\b|insight|intelligence|reporting|\breports?\b|\bbi\b|\bmis\b|dashboard|"
                           r"power (?:bi|platform)|metrics?\b|\bkpi|statistic", re.I)
TITLE_ADJ_RX = re.compile(r"performance(?! marketing)|planning|planner|forecast|excellence|research|\bfp&a\b", re.I)
TITLE_OFF_RX = re.compile(r"account executive|account manager|key accounts?|supervisor|\bsales (?:executive|manager|associate|"
                          r"supervisor|representative)|security|\bgrc\b|audit|controller|customer care|merchandiser|recruit|"
                          r"talent|people partner|health (?:&|and) safety|community manag|social media|product owner", re.I)
# Titles that are clearly your kind of role even when only the title is known (job-alert e-mails, aggregators)
STRONG_TITLE_RX = re.compile(r"power ?bi|\bbi\b|business intelligence|data analy|analytics engineer|reporting (?:analyst|specialist|lead|developer)|"
                             r"data visuali[sz]|dashboard|insights? analyst|\bmis\b|tableau|qlik|looker|analytics (?:analyst|specialist|lead|consultant)|"
                             r"data (?:&|and) analytics|sql (?:analyst|developer)|snowflake|microsoft fabric|\bfabric\b", re.I)
FOREIGN_TITLE_RX = re.compile(r"\b(saudi(?: arabia)?|ksa|riyadh|jeddah|dammam|al ?khobar|neom|qatar|doha|kuwait|bahrain|manama|"
                              r"oman|muscat|egypt|cairo|jordan|amman|lebanon|beirut|turkey|istanbul|morocco|casablanca|"
                              r"pakistan|karachi|lahore|india|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|london)\b", re.I)

AGENCY_RX = re.compile(r"\b(our client|on behalf of (?:our|a) client|recruitment (?:agency|consultancy)|staffing|"
                       r"we are recruiting for|leading client|confidential client|headhunt)", re.I)


def _cosine(a: str, b: str) -> float:
    ta = Counter(t for t in tokens(a) if t not in STOP and len(t) > 2)
    tb = Counter(t for t in tokens(b) if t not in STOP and len(t) > 2)
    if not ta or not tb:
        return 0.0
    wa = {k: 1 + math.log(v) for k, v in ta.items()}
    wb = {k: 1 + math.log(v) for k, v in tb.items()}
    dot = sum(wa[k] * wb[k] for k in wa.keys() & wb.keys())
    return dot / (math.sqrt(sum(v * v for v in wa.values())) * math.sqrt(sum(v * v for v in wb.values())))


def required_years(text: str) -> tuple[float | None, float | None]:
    """Smallest 'minimum years' requirement mentioned near the word experience (min, max)."""
    cands = []
    for m in YEARS_RX.finditer(text or ""):
        lo, hi = int(m.group(1)), int(m.group(2)) if m.group(2) else None
        if lo <= 25:
            cands.append((lo, hi))
    for m in MIN_YEARS_RX.finditer(text or ""):
        cands.append((int(m.group(1)), None))
    if not cands:
        return None, None
    lo, hi = max(cands, key=lambda x: x[0])  # the headline (largest) minimum is the real ask
    return float(lo), (float(hi) if hi else None)


def level_of(title: str) -> str:
    t = title.lower()
    for lvl, rx in LEVELS:
        if re.search(rx, t):
            return lvl
    return "analyst"


def label_for(score: float) -> str:
    return ("Exceptional match" if score >= 9 else "Strong match" if score >= 8 else "Good match" if score >= 7
            else "Possible match" if score >= 6 else "Low priority")


def detect_agency(text: str, company: dict) -> bool:
    return bool(AGENCY_RX.search(text or "")) or bool(re.search(r"recruit|staffing|talent solutions|headhunt",
                                                                company.get("industry", "") or "", re.I))


def score_job(job: dict, company: dict, settings, llm_result: dict | None = None) -> dict:
    profile, tax = settings.profile, settings.taxonomy
    weights = settings.get("scoring", "weights")
    title = job.get("title") or ""
    desc = job.get("description") or ""
    text = f"{title}\n{desc}"
    ntitle = normalize_title(title)
    loc = classify(job.get("location") or "", job.get("country_code"), desc)
    tier = job.get("location_tier") or loc["tier"]
    t_loc = classify(title)  # a title like "Manager, Abu Dhabi" overrides a generic board location
    if t_loc["in_uae"] and t_loc["tier"] == "uae_other" and tier in ("dubai", "uae_unspecified"):
        tier, loc = "uae_other", t_loc
    has_desc = len(desc) >= 200

    # ---------------------------------------------------------------- Stage A
    family, fit = tax.match_title(ntitle)
    jd_skills = {k: v for k, v in extract_skills(text).items() if k != "arabic"}
    signal = len(CORE_SIGNAL & jd_skills.keys()) + sum(1 for rx in RESP_POS_RX.values() if rx.search(desc)) // 3
    negative_title = tax.is_negative(ntitle)
    min_sig = int(settings.get("scoring", "stage_a_min_signal", 3))
    neg_counts = {k: len(rx.findall(desc)) for k, rx in RESP_NEG_RX.items()}
    neg_heavy = [k for k, c in neg_counts.items() if c >= 2]
    if has_desc and neg_heavy and signal < 4 and fit < 1.0:
        stage_a, reason = False, f"Role centres on {neg_heavy[0]} rather than BI/analytics"
    elif fit >= 0.6 and not (negative_title and signal < min_sig) and not (fit <= 0.6 and has_desc and signal < 2):
        stage_a, reason = True, f"Title matches '{family}'"
    elif negative_title and signal < 5:
        stage_a, reason = False, "Title is outside target families and the description lacks BI/analytics content"
    elif signal >= min_sig:
        stage_a, reason = True, f"Off-taxonomy title rescued by description ({signal} analytics signals)"
        family, fit = family or "Semantic match", max(fit, 0.6)
    elif not has_desc and fit == 0:
        stage_a, reason = False, "No description and title not in taxonomy"
    else:
        stage_a, reason = False, f"Not an analytics role (only {signal} analytics signals)"

    e = elig.assess(title, desc, tier, profile, settings.raw.get("locations", {}), job.get("status", "active"))
    foreign = FOREIGN_TITLE_RX.search(title)
    if foreign and not t_loc["in_uae"]:  # e.g. "Developer (Saudi Arabia)" listed under a Dubai board location
        tier = "outside"
    if tier == "outside":
        stage_a, reason = False, ("Title names a location outside the UAE (%s)" % foreign.group(0)) if foreign and not t_loc["in_uae"] \
            else "Location is outside the UAE"

    # ---------------------------------------------------------------- technical
    prof = profile["skills"]
    matched, partial, missing = [], [], []
    counts = skill_counts(desc)
    if jd_skills:
        num = den = 0.0
        for sid, imp in jd_skills.items():
            w = 0.6 + 0.4 * min(1.0, SKILLS[sid][1])  # every named tool matters for the technical fit
            iw = (1.0 if imp == "required" else 0.5) * (1 + 0.5 * (min(3, counts.get(sid, 1)) - 1))  # repeated = central
            p = float(prof.get(sid, 0.0))
            num += w * iw * p
            den += w * iw
            (matched if p >= 0.7 else partial if p >= 0.3 else missing).append(sid)
        ratio = num / den if den else 0
        bonus = min(1.2, sum(0.4 for s in ("power_bi", "sql", "dax", "snowflake", "microsoft_fabric") if s in jd_skills))
        technical = min(10.0, 10 * ratio + bonus)
        if len(jd_skills) <= 3:
            technical = min(technical, 7.5)
    else:
        technical = 4.0 if fit >= 0.8 else 2.5

    # ---------------------------------------------------------------- responsibilities
    pos = [k for k, rx in RESP_POS_RX.items() if rx.search(desc)]
    neg = [k for k, rx in RESP_NEG_RX.items() if rx.search(desc)]
    if has_desc:
        coverage = len(pos) / (len(pos) + 1.5 * len(neg)) if (pos or neg) else 0.0
        density = min(1.0, len(pos) / 7)
        cos = min(1.0, _cosine(desc, profile["experience_text"]["text"]) / 0.30)
        responsibilities = 10 * (0.35 * coverage + 0.35 * density + 0.30 * cos)
    else:
        responsibilities = 5.0 * fit + 1.0

    # ---------------------------------------------------------------- experience & seniority
    cand = float(profile["candidate"]["years_experience"])
    lo, hi = required_years(desc)
    lvl = level_of(title)
    sen = 10 * float(tax.seniority.get(lvl, 1.0))
    if lo is not None:
        gap = lo - cand
        ys = 10 if gap <= 0.5 else 8.5 if gap <= 1.0 else 7.0 if gap <= 1.5 else 5.0 if gap <= 2.5 else 3.5 if gap <= 3.5 else 2.0
        if hi is not None and hi < cand - 1.5:
            ys = min(ys, 7.0)  # overqualified band (e.g. 0–2 yrs)
        experience = 0.6 * ys + 0.4 * sen
    else:
        experience = sen
    if e.exp_cap is not None and e.exp_cap < cand:
        experience = min(experience, 6.0)

    # ---------------------------------------------------------------- eligibility
    eligibility = 0.0 if e.hard else max(0.0, 10.0 - e.penalty - 0.5 * len(e.uncertain))

    # ---------------------------------------------------------------- industry
    ind_text = f"{company.get('industry', '')} {company.get('name', '')}"
    industry = 6.0
    for rx, s in INDUSTRY_SCORE:
        if re.search(rx, ind_text, re.I):
            industry = float(s)
            break
    if re.search(r"asset management|\baum\b|portfolio|investment management|fund", desc, re.I):
        industry = max(industry, 9.5)

    # ---------------------------------------------------------------- career
    career = 6.0 + {1: 2.0, 2: 1.0}.get(int(company.get("priority") or 3), 0.0)
    if re.search(r"graduate|programme|program|trainee|development", title, re.I):
        career += 1.5
    if lvl == "intern":
        career -= 0.5 if re.search(r"paid|stipend|salary|allowance", desc, re.I) else 1.5
    if re.search(r"contract|temporary|fixed[- ]term", f"{job.get('employment_type') or ''} {title}", re.I):
        career -= 1.0
    if fit >= 1.0:
        career += 1.0
    is_agency = bool(job.get("is_agency")) or detect_agency(desc, company)
    # a recruiter's title-only alert for a strong BI title is worth a ping like any other lead – no agency penalty there
    agency_pen = is_agency and (has_desc or not STRONG_TITLE_RX.search(title))
    if agency_pen:
        career -= 1.5
    career = max(1.0, min(10.0, career))

    sub = {"technical": technical, "responsibilities": responsibilities, "experience": experience,
           "eligibility": eligibility, "industry": industry, "career": career}

    # ---------------------------------------------------------------- optional LLM blend (never touches hard rules)
    method = "rules"
    if llm_result and llm_result.get("subscores"):
        blend = float(settings.get("llm", "blend", 0.5))
        for k in ("technical", "responsibilities", "experience", "industry", "career"):
            v = llm_result["subscores"].get(k)
            if isinstance(v, (int, float)):
                sub[k] = (1 - blend) * sub[k] + blend * max(0.0, min(10.0, float(v)))
        for flag in llm_result.get("eligibility_flags", []) or []:
            e.uncertain.append(f"LLM flagged: {flag}")
        method = "rules+llm"

    sub = {k: round(max(0.0, min(10.0, v)), 2) for k, v in sub.items()}
    raw = sum(sub[k] * float(weights[k]) for k in weights)
    final = 1 + 0.9 * raw
    if agency_pen:
        final -= float(settings.get("scoring", "agency_penalty", 0.3))
    if not stage_a:
        final = min(final, 5.9)
    caps = []
    if stage_a and reason.startswith("Off-taxonomy"):
        # the title isn't a BI/analytics title – the description alone may not lift it to an alert
        if TITLE_OFF_RX.search(title):
            caps.append((6.4, "title is not an analytics role (description mentions data tools)"))
        elif TITLE_DATA_RX.search(title):
            caps.append((8.4, "analytics-adjacent title"))
        elif TITLE_ADJ_RX.search(title):
            caps.append((7.4, "planning/performance title – check how much of the role is BI"))
        elif signal >= 5 and jd_skills.keys() & {"power_bi", "dax", "microsoft_fabric"}:
            caps.append((6.9, "non-analytics title with BI duties – in the digest, not an instant alert"))
        else:
            caps.append((6.4, "title is not an analytics role (description mentions data tools)"))
    if not has_desc:
        if job.get("is_official") and (job.get("extra") or {}).get("via") == "employer alert e-mail":
            # official posting announced by the employer's own alert e-mail, site can't be read → still worth a ping
            caps.append((float(settings.get("scoring", "official_title_only_cap", 7.4)),
                         "title-only (from the employer's own alert e-mail) – open the posting to check details"))
        elif STRONG_TITLE_RX.search(title) and not TITLE_OFF_RX.search(title):
            # strong BI/analytics title from a job-alert e-mail → ping now (being early matters), labelled as title-only
            caps.append((float(settings.get("scoring", "strong_title_only_cap", 7.4)),
                         "title-only (from a job-alert e-mail) – strong title match; open the posting to check details"))
        else:
            caps.append((6.9, "title-only estimate (no full description)"))
    if lo is not None and lo - cand >= 3.0:
        caps.append((6.9, f"asks for {int(lo)}+ years"))
    elif lo is not None and lo - cand >= 1.4:
        caps.append((7.9, f"asks for {int(lo)}+ years"))
    if tier == "uae_other":
        caps.append((float(settings.get("locations", "uae_other_max_score", 6.9)), "outside Dubai (other emirate)"))
    elif tier == "remote":
        caps.append((7.4, "remote role – Dubai base unconfirmed"))
    if neg_heavy:
        caps.append((6.9, f"core of the role is {neg_heavy[0]}"))
    if lvl in ("head", "director", "vp", "chief"):
        caps.append((5.9, f"{lvl}-level role"))
    elif lvl in ("manager", "lead"):
        caps.append((7.9, f"{lvl}-level role"))
    for cap, why in caps:
        if final > cap:
            final = cap
    final = round(max(1.0, min(10.0, final)), 1)

    # ---------------------------------------------------------------- explanations
    reasons, gaps = [], []
    core_hits = [HUMAN[s] for s in ("power_bi", "sql", "dax", "snowflake", "microsoft_fabric", "python", "excel", "azure") if s in jd_skills]
    if core_hits:
        reasons.append("Uses your core stack: " + ", ".join(core_hits[:5]))
    if pos:
        reasons.append("Duties you've already delivered: " + ", ".join(pos[:4]))
    if lo is not None and lo <= cand + 0.5:
        reasons.append(f"Experience ask ({int(lo)}{'–' + str(int(hi)) if hi else '+'} yrs) fits your ~{cand:g} yrs")
    if industry >= 9.5 and re.search(r"asset|fund|portfolio|investment", desc + ind_text, re.I):
        reasons.append("Investment/asset-management domain matches your background")
    elif industry >= 9:
        reasons.append(f"Financial-services domain ({company.get('industry')}) aligns with your background")
    if llm_result and llm_result.get("why"):
        reasons = list(dict.fromkeys(reasons + llm_result["why"]))
    for s in missing + partial:
        if jd_skills.get(s) == "required" and s != "arabic":
            gaps.append(f"{HUMAN[s]} ({'not in CV' if s in missing else 'only transferable experience'})")
    if lo is not None and lo > cand + 0.5:
        gaps.append(f"Asks for {int(lo)}+ yrs (you have ~{cand:g})")
    if lvl in ("lead", "manager", "head", "director", "vp", "chief"):
        gaps.append(f"Seniority: {lvl}-level title")
    gaps += [f"Includes {n} duties" for n in (neg_heavy or neg)[:2]]
    gaps += [f"Score capped: {w}" for c, w in caps if final >= c - 0.001 and "title-only" not in w]
    gaps += e.preferences
    if llm_result and llm_result.get("gaps"):
        gaps = list(dict.fromkeys(gaps + llm_result["gaps"]))
    if not has_desc:
        gaps.append("Full description not available – score based on title/snippet")

    eligible = not e.hard
    if not eligible:
        rec = "Not eligible – " + "; ".join(e.hard)
    elif not stage_a:
        rec = "Skip – " + reason
    elif final >= 8:
        rec = "Apply now – strong fit"
    elif final >= 7:
        rec = "Apply – tailor CV to the gaps listed"
    elif final >= 6:
        rec = "Consider – borderline fit"
    else:
        rec = "Low priority"
    conf_pts = (2 if len(desc) >= 1500 else 1 if len(desc) >= 400 else 0) + (1 if len(jd_skills) >= 5 else 0) \
        - (1 if len(e.uncertain) >= 2 else 0) + (1 if method == "rules+llm" else 0)
    confidence = "High" if conf_pts >= 3 else "Medium" if conf_pts >= 1 else "Low"

    return {
        "score": final, "label": "Ineligible (hard restriction)" if not eligible else label_for(final),
        "eligible": eligible, "stage_a_pass": stage_a, "stage_a_reason": reason, "family": family,
        "hard_disqualifiers": e.hard, "uncertainties": e.uncertain + e.notes, "subscores": sub,
        "matched_skills": [HUMAN[s] for s in matched], "missing_skills": [HUMAN[s] for s in missing + partial],
        "reasons": reasons[:4], "gaps": gaps[:6], "recommendation": rec, "confidence": confidence,
        "method": method, "is_agency": is_agency, "location_tier": tier, "city": loc["city"],
        "required_years": lo, "level": lvl, "caps": [w for c, w in caps],
    }

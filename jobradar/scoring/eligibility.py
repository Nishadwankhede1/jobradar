"""Deterministic eligibility rules. Hard restrictions here can never be overridden by any similarity/LLM score."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..region import is_eu, place_label
from .skills import PREFERRED_MARKERS

SENT_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+|•")

# Emiratisation programmes: Sinyar = Al-Futtaim's graduate track for UAE nationals ("SGP - UAEN…")
NATIONALS_TITLE = re.compile(r"uae ?nationals?|\bemirati|\buaen\d?\b|nationals? only|\(national\)|\btawteen\b|\bnafis\b|\bsinyar\b|\bsgp\b", re.I)
NATIONALS_HARD = re.compile(
    r"(only (?:open|available) (?:to|for) (?:uae|emirati) nationals?|(?:uae|emirati) nationals? only|exclusively for (?:uae|emirati)|"
    r"must be (?:a |an )?(?:uae national|emirati)|(?:this|the) (?:role|position|vacancy|programme|program|opportunity) is (?:open )?(?:only )?(?:for|to) (?:uae|emirati) nationals?|"
    r"open to (?:uae|emirati) nationals? only|reserved for (?:uae|emirati) nationals|emiratisation (?:role|position|vacancy)|"
    r"(?:uae|emirati) national(?:ity)? (?:is )?(?:required|mandatory|a must)|emirati talent only|"
    r"(?:for|to|targets?|targeting) (?:young |talented )?(?:uae national|emirati) (?:fresh |recent |young )?(?:graduates?|talents?|youth)|"
    r"\bemirati (?:fresh |recent )graduates?|uae national (?:fresh |recent )graduates?)", re.I)
NATIONALS_SOFT = re.compile(r"(uae|emirati) nationals? (?:are )?(?:strongly )?(?:encouraged|preferred|will be given preference|priority)", re.I)

ARABIC = re.compile(r"\barabic\b", re.I)
ARABIC_HARD = re.compile(
    r"(fluen\w*|proficien\w*|native|excellent|strong)[^.\n]{0,40}\barabic\b[^.\n]{0,40}(required|mandatory|essential|must|is a must)|"
    r"\barabic\b[^.\n]{0,25}(is |are )?(mandatory|required|essential|a must|must)|"
    r"must (?:be able to )?(?:speak|read|write|communicate in)[^.\n]{0,20}\barabic|arabic speakers? only|native arabic speaker|"
    r"bilingual[^.\n]{0,30}(english and arabic|arabic and english)", re.I)

CERTS = {"CPA": r"\bcpa\b", "ACCA": r"\bacca\b", "CFA charter": r"\bcfa\b", "Chartered Accountant": r"\bchartered accountant|\bca qualified|\bcima\b",
         "FRM": r"\bfrm\b", "PMP": r"\bpmp\b", "CISSP": r"\bcissp\b", "medical licence": r"\b(dha|moh|haad|doh) licen[cs]e"}
CERT_REQ = re.compile(r"(required|mandatory|must|essential|qualified)", re.I)

OTHER_LANGS = ["mandarin", "chinese", "cantonese", "russian", "french", "german", "turkish", "spanish", "italian",
               "portuguese", "japanese", "korean", "farsi", "persian", "urdu", "hebrew", "dutch", "polish"]
LANG_TITLE = re.compile(r"\(?\b(arabic|mandarin|chinese|russian|french|german|turkish|spanish|italian|portuguese|japanese|korean|farsi)[- ](speak(?:er|ing)|speaker)s?\b", re.I)
CLEARANCE = re.compile(r"security clearance|sc clearance|dv clearance|government clearance required", re.I)
MAX_EXP = re.compile(r"(?:no more than|maximum(?: of)?|max\.?|not more than|up to|less than)\s*(\d{1,2})\s*\+?\s*years?(?: of)?(?: (?:professional|work|full[- ]time))? experience", re.I)
GRAD_YEAR = re.compile(r"(?:graduat\w*|class of|degree (?:completed|obtained))[^.\n]{0,40}?(20\d\d)(?:\s*(?:-|–|to|and|or)\s*(20\d\d))?", re.I)
ENROLLED = re.compile(r"currently (?:enrolled|pursuing|studying)|enrolled in (?:a |an )?(?:bachelor|master|degree|university)|final[- ]year student|must be a (?:current )?student", re.I)
VISA_NO_SPONSOR = re.compile(r"(no|not|unable to|cannot) (?:provide |offer )?(?:visa )?sponsor|own (?:residence )?visa|must hold (?:a )?valid (?:uae )?(?:residence |work )?visa|visa sponsorship (?:is )?not", re.I)
RESIDENT_REQ = re.compile(r"(must be|currently) (?:based|residing|located) in (?:the )?(?:uae|dubai)|uae resident", re.I)
DEGREE_STRICT = re.compile(r"(?:bachelor'?s?|degree)[^.\n]{0,40}(?:in|of) (computer science|statistics|mathematics|economics|finance|accounting|actuarial)[^.\n]{0,60}(?:required|must|mandatory)?", re.I)
MASTERS_REQ = re.compile(r"master'?s(?: degree)?[^.\n]{0,30}(required|mandatory|must)|(require|must have)[^.\n]{0,20}master'?s", re.I)


@dataclass
class Eligibility:
    hard: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    penalty: float = 0.0
    exp_cap: int | None = None


def _edu(profile: dict) -> tuple[str, str, str]:
    """(short education summary, in-progress institution, bachelor's year) – all from profile.toml, nothing hard-coded."""
    degs = (profile.get("education") or {}).get("degrees") or []
    parts, inst, year = [], "", ""
    for d in degs:
        lvl = {"masters": "Master's", "bachelors": "Bachelor's", "phd": "PhD"}.get(d.get("level"), d.get("level", ""))
        parts.append(f"{d.get('field', '')} {lvl}{' in progress' if d.get('status') == 'in progress' else ''}".strip())
        if d.get("status") == "in progress" and not inst:
            inst = d.get("institution", "")
        if d.get("level") == "bachelors" and d.get("year"):
            year = str(d["year"])
    return " + ".join(parts) or "see your CV", inst, year


def assess(title: str, description: str, location_tier: str, profile: dict, loc_penalties: dict,
           status: str = "active") -> Eligibility:
    e = Eligibility()
    text = description or ""
    cand_years = float(profile["candidate"]["years_experience"])
    langs = {l.lower() for l in profile["candidate"]["languages"]}
    certs_held = " ".join(profile.get("certifications", {}).get("held", [])).lower()

    if status != "active":
        e.hard.append("Vacancy is closed / no longer listed")
    if location_tier == "outside" and loc_penalties.get("hard_reject_outside_uae", True):
        e.hard.append(f"Location is outside {place_label()}")
    e.penalty += float(loc_penalties.get(location_tier, 0.0) or 0.0)
    if location_tier == "uae_other":
        e.preferences.append("Based outside Dubai (other emirate) – commute/relocation needed")
    elif location_tier == "uae_unspecified":
        e.uncertain.append("Exact UAE city not stated")

    # nationality restriction (Emiratisation)
    title_pref = bool(re.search(r"nationals?\s*(?:are\s*)?(?:preferred|encouraged|a plus)", title or "", re.I))
    if title_pref:
        e.uncertain.append("UAE nationals preferred (title) – not a hard restriction")
        e.penalty += 1.0
    elif NATIONALS_TITLE.search(title or "") or NATIONALS_HARD.search(text):
        e.hard.append("Restricted to UAE nationals (Emiratisation role)")
    elif NATIONALS_SOFT.search(text):
        e.uncertain.append("UAE nationals preferred")
        e.penalty += 1.0

    # Arabic
    if "arabic" not in langs:
        for sent in SENT_SPLIT.split(text):
            if not ARABIC.search(sent):
                continue
            if PREFERRED_MARKERS.search(sent) and not re.search(r"must|mandatory|essential", sent, re.I):
                if "Arabic preferred (you list English/Hindi)" not in e.preferences:
                    e.preferences.append("Arabic preferred (you list English/Hindi)")
                    e.penalty += 1.0
            elif ARABIC_HARD.search(sent):
                e.hard.append("Fluent Arabic is mandatory (not in your CV)")
                break

    mt = LANG_TITLE.search(title or "")
    if mt and mt.group(1).lower() not in langs:
        e.hard.append(f"{mt.group(1).title()} speaker required (title) – not in your CV")
    for lang in OTHER_LANGS:
        if lang in langs or not re.search(rf"\b{lang}\b", text, re.I):
            continue
        for sent in SENT_SPLIT.split(text):
            if not re.search(rf"\b{lang}\b", sent, re.I):
                continue
            if PREFERRED_MARKERS.search(sent) and not re.search(r"must|mandatory|essential|required", sent, re.I):
                e.preferences.append(f"{lang.title()} preferred")
                e.penalty += 0.5
            elif re.search(rf"(fluen\w*|proficien\w*|native|speak\w*)[^.\n]{{0,40}}\b{lang}\b|\b{lang}\b[^.\n]{{0,30}}(required|mandatory|essential|must)", sent, re.I):
                e.hard.append(f"Fluent {lang.title()} required – not in your CV")
            break

    if CLEARANCE.search(text):
        e.hard.append("Requires security clearance")

    for name, rx in CERTS.items():
        for sent in SENT_SPLIT.split(text):
            if re.search(rx, sent, re.I) and CERT_REQ.search(sent) and name.lower().split()[0] not in certs_held:
                if PREFERRED_MARKERS.search(sent):
                    e.preferences.append(f"{name} preferred")
                    e.penalty += 0.5
                else:
                    e.hard.append(f"Mandatory certification/licence: {name}")
                break

    m = MAX_EXP.search(text)
    if m:
        cap = int(m.group(1))
        e.exp_cap = cap
        if cap < cand_years:
            sent = next((s for s in SENT_SPLIT.split(text) if m.group(0) in s), m.group(0))
            if re.search(r"no more than|maximum|not more than", m.group(0), re.I) and cap <= 2:
                e.hard.append(f"Experience cap: max {cap} years (you have ~{cand_years:g})")
            else:
                e.uncertain.append(f"Programme targets ≤{cap} years' experience (you have ~{cand_years:g}) – check eligibility")
                e.penalty += 1.5

    g = GRAD_YEAR.search(text)
    if g and re.search(r"graduate|programme|program|intern|trainee|scheme", (title or "") + " " + text[:400], re.I):
        years = [int(y) for y in g.groups() if y]
        _, _, by = _edu(profile)
        e.uncertain.append(f"Graduation window {'–'.join(map(str, years))}: your Bachelor's is {by or 'older'}; a degree in progress may qualify – confirm expected completion date")
        e.penalty += 1.0

    if ENROLLED.search(text):
        if profile["candidate"].get("current_student"):
            inst = _edu(profile)[1]
            e.notes.append(f"Requires current enrolment – you qualify via your degree in progress{f' ({inst})' if inst else ''}")
        else:
            e.hard.append("Requires current student enrolment")

    if is_eu():
        from .sponsor import assess_eu
        assess_eu(e, title or "", text, location_tier)
    elif VISA_NO_SPONSOR.search(text):
        e.uncertain.append("Employer states no visa sponsorship / own visa required – confirm your visa status covers employment")
        e.penalty += 0.5
    if RESIDENT_REQ.search(text):
        e.notes.append("Requires UAE/Dubai residence – you are Dubai-based")

    d = DEGREE_STRICT.search(text)
    if d:
        e.preferences.append(f"Prefers a degree in {d.group(1).title()} (yours: {_edu(profile)[0]})")
        e.penalty += 0.75
    if MASTERS_REQ.search(text):
        e.uncertain.append("Master's degree required – yours is in progress")
        e.penalty += 0.75

    e.hard = list(dict.fromkeys(e.hard))
    return e

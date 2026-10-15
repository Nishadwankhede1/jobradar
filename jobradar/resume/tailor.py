"""Tailor the master CV (config/resume.json) to one job description – truthfully.

What it does
  * finds the job's keywords (tools, methods, BI phrases) with the same lexicon the scorer uses;
  * keywords you really have (CV text, profile proficiency ≥ 0.6, or listed in resume.json → extra_skills) are
    written into the Skills section in the job ad's own wording, moved to the front of their line, and the strongest
    ones lead the Summary's "Expert in …" list;
  * experience bullets are re-ordered so the most relevant ones come first (wording, numbers and dates never change);
  * keywords you don't have are NOT added – they are reported as gaps so you can decide.
Nothing is invented: no new jobs, titles, employers, dates, metrics, degrees or languages.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path

from ..scoring.skills import COMPILED, HUMAN, extract_skills

ADD_MIN_PROFICIENCY = 0.7  # same bar the scorer uses for "skills you have"

# where an added keyword goes (label of the Skills line in resume.json)
CATEGORY = {
    "BI and Reporting": {"power_bi", "dax", "power_query", "excel", "kpi", "dashboards", "reporting_automation", "data_visualization",
                         "predictive_analytics", "forecasting", "statistics", "regression", "ssrs", "tableau", "looker", "qlik",
                         "sales_analytics", "financial_data", "asset_management", "vba", "fpa", "financial_modeling"},
    "Data and Cloud": {"sql", "sql_server", "snowflake", "python", "pandas", "azure", "microsoft_fabric", "azure_data_factory", "etl",
                       "data_modeling", "data_warehouse", "cloud_data", "ssis", "databricks", "spark", "bigquery", "redshift", "aws",
                       "gcp", "dbt", "airflow", "r_lang", "sas", "machine_learning", "api_integration", "sap", "alteryx"},
    "Migrations": {"data_migration", "salesforce", "dynamics_365", "crm_analytics"},
    "Delivery": {"ci_cd", "git", "agile", "stakeholder_management", "requirements_gathering", "business_analysis", "data_quality",
                 "data_governance"},
}
# skill ids whose regex is too loose to copy the ad's wording (e.g. "funds", "migrating") → use a clean name instead
CANONICAL = {"asset_management": "Asset Management", "data_migration": "Data Migration", "crm_analytics": "CRM",
             "api_integration": "REST APIs", "stakeholder_management": "Stakeholder Management", "forecasting": "Forecasting",
             "statistics": "Statistics", "regression": "Regression Modeling", "cloud_data": "Cloud Data Platforms",
             "data_quality": "Data Quality", "agile": "Agile/Scrum", "git": "Git", "reporting_automation": "Reporting Automation",
             "kpi": "KPIs", "dashboards": "Dashboards", "financial_data": "Financial Data", "etl": "ETL",
             "sales_analytics": "Sales Analytics", "machine_learning": "Machine Learning", "excel": "Advanced Excel",
             "requirements_gathering": "Requirements Gathering", "business_analysis": "Business Analysis",
             "data_governance": "Data Governance", "ci_cd": "CI/CD"}
# technical tools allowed into the Summary's "Expert in …" list
SUMMARY_TOOLS = {"power_bi", "dax", "power_query", "sql", "sql_server", "snowflake", "excel", "python", "azure", "microsoft_fabric",
                 "azure_data_factory", "etl", "data_modeling", "salesforce", "data_warehouse"}
ACRONYMS = {"sql", "dax", "etl", "elt", "kpi", "kpis", "bi", "ssrs", "ssis", "adf", "aws", "gcp", "dwh", "crm", "api", "apis",
            "ci/cd", "mdm", "sfdc", "d365", "brd", "t-sql", "pl/sql", "vba", "sap", "fp&a", "ml", "ai", "mis", "ms"}

# common BI phrases ATS screens look for, each mapped to the CV evidence that makes it true for you
PHRASES = [
    ("Business Intelligence", r"business intelligence", r"business intelligence"),
    ("Data Analysis", r"data analys[ie]s", r"analys|sql|power bi"),
    ("Executive Reporting", r"executive (?:reporting|dashboards?|reports?)|c-suite|leadership reporting|management reporting",
     r"ceo/coo reporting|leadership reporting|board reviews"),
    ("Management Reporting", r"management reporting|\bmis\b", r"ceo/coo reporting|leadership reporting"),
    ("Data Pipelines", r"data pipelines?", r"pipelines"),
    ("Process Automation", r"process automation|automat(?:e|ion) (?:of )?(?:manual )?processes", r"automat"),
    ("Cloud Migration", r"cloud migration", r"migrations? on azure"),
    ("Data Cleaning", r"data (?:cleaning|cleansing|wrangling|preparation)", r"cleaning"),
    ("Market Intelligence", r"market intelligence", r"market intelligence"),
    ("Data Accuracy", r"data accuracy", r"data accuracy"),
    ("Self-Service Reporting", r"self[- ]service", r"adopted by 15\+ business units"),
    ("Ad-hoc Analysis", r"ad[- ]hoc (?:analysis|reporting|requests)", r"sql market intelligence|analysis"),
]


def load_master(root: Path) -> dict:
    return json.loads((root / "config" / "resume.json").read_text(encoding="utf-8"))


def _flat(cv: dict) -> str:
    parts = [cv["headline"], cv["summary"]["lead"], " ".join(cv["summary"]["expert_in"])]
    parts += [", ".join(s["items"]) for s in cv["skills"]]
    for e in cv["experience"]:
        parts += [e["title"], e["company"], *e["bullets"]]
    parts += [d["degree"] + " " + d["rest"] for d in cv["education"]] + cv["certifications"] + [cv["languages"]]
    return "\n".join(parts)


def _nice(form: str) -> str:
    words = form.strip().split()
    out = []
    for w in words:
        lw = w.lower()
        out.append(w.upper() if lw in ACRONYMS else w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:])
    return " ".join(out)


def _jd_form(sid: str, jd: str) -> str:
    """The ad's own wording for a skill (most frequent full-word match), cleaned up."""
    if sid in CANONICAL:
        return CANONICAL[sid]
    forms = Counter()
    for m in COMPILED[sid].finditer(jd):
        a, b = m.start(), m.end()
        while a > 0 and (jd[a - 1].isalnum()):
            a -= 1
        while b < len(jd) and (jd[b].isalnum()):
            b += 1
        f = re.sub(r"\s+", " ", jd[a:b]).strip(" .,;:()")
        if 2 <= len(f) <= 40 and len(f.split()) <= 4:
            forms[f.lower()] += 1
    if not forms:
        return _nice(HUMAN.get(sid, sid))
    form = forms.most_common(1)[0][0]
    human = HUMAN.get(sid, "")
    if human and re.sub(r"[^a-z0-9]", "", form) == re.sub(r"[^a-z0-9]", "", human.lower()):
        return human if any(c.isupper() for c in human) else _nice(human)  # "powerbi" → "Power BI"
    if sid == "data_visualization" and "visual" not in form:
        return "Data Visualization"
    return _nice(form)


def _literal(term: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", text.lower()) is not None


def _norm(s: str) -> str:
    s = s.lower().replace("modelling", "modeling").replace("isation", "ization").replace("analyse", "analyze")
    return re.sub(r"[^a-z0-9+#&]", "", s)


def _present(term: str, text: str) -> bool:
    """ATS-style presence: exact phrase, or the same words ignoring case/spacing/UK-US spelling ("PowerBI" = "Power BI")."""
    return _literal(term, text) or (len(_norm(term)) >= 3 and _norm(term) in _norm(text))


def tailor(master: dict, job: dict, proficiency: dict) -> tuple[dict, dict]:
    """Return (tailored_cv, report). `job` needs title + description."""
    cv = copy.deepcopy(master)
    title, desc = job.get("title") or "", job.get("description") or ""
    jd = f"{title}\n{desc}"
    base_text = _flat(master)
    extra = {(x.get("id") or "").lower(): x for x in master.get("extra_skills") or []}
    extra_names = {(x.get("name") or "").lower() for x in master.get("extra_skills") or []}

    # ---- keywords in the ad, most important first (required > preferred, then frequency, then BI weight)
    found = {k: v for k, v in extract_skills(jd).items() if k != "arabic"}
    freq = {k: len(COMPILED[k].findall(jd)) for k in found}
    order = sorted(found, key=lambda k: (found[k] != "required", -freq[k], k))
    keywords = []  # (term, kind, sid)
    for sid in order:
        has = (float(proficiency.get(sid, 0)) >= ADD_MIN_PROFICIENCY or bool(COMPILED[sid].search(base_text))
               or sid in extra or HUMAN.get(sid, "").lower() in extra_names)
        keywords.append({"term": _jd_form(sid, jd), "sid": sid, "has": has, "human": HUMAN.get(sid, sid)})
    for name, rx_jd, rx_cv in PHRASES:
        if re.search(rx_jd, jd, re.I):
            keywords.append({"term": name, "sid": None, "has": bool(re.search(rx_cv, base_text, re.I)), "human": name})
    for x in master.get("extra_skills") or []:  # your own declared extras that the ad mentions by name
        n = x.get("name") or ""
        if n and _literal(n, jd) and not any(k["term"].lower() == n.lower() for k in keywords):
            keywords.append({"term": n, "sid": x.get("id"), "has": True, "human": n, "category": x.get("category")})

    # ---- skills section: move matches to the front, add missing-but-true terms in the ad's wording
    lines = {s["label"]: s for s in cv["skills"]}
    added, matched, missing = [], [], []
    for k in keywords:
        term = k["term"]
        if not k["has"]:
            missing.append(k)
            continue
        if _present(term, base_text):
            matched.append(k)
            continue
        cat = k.get("category") or next((c for c, ids in CATEGORY.items() if k["sid"] in ids), "BI and Reporting")
        line = lines.get(cat) or cv["skills"][0]
        if not any(_present(term, it) for it in line["items"]):
            line["items"].append(term)
        added.append(k)
    rank = {k["term"].lower(): i for i, k in enumerate(keywords)}

    def item_rank(item: str) -> int:
        hits = [rank[t] for t in rank if _literal(t, item)]
        sids = [i for i, k in enumerate(keywords) if k["sid"] and COMPILED.get(k["sid"]) and COMPILED[k["sid"]].search(item)]
        return min(hits + sids) if (hits or sids) else 999

    for s in cv["skills"]:
        s["items"] = sorted(s["items"], key=item_rank)  # stable: untouched items keep their original order

    # ---- summary: lead with the tools this ad cares about (only ones you are expert in)
    exp = cv["summary"]["expert_in"]
    for k in keywords:
        if k["has"] and k["sid"] in SUMMARY_TOOLS and float(proficiency.get(k["sid"], 0)) >= 0.9 and len(exp) < 7 \
                and not any(_present(k["term"], e) or COMPILED[k["sid"]].search(e) for e in exp):
            exp.append(k["term"])
    cv["summary"]["expert_in"] = sorted(exp, key=item_rank)

    # ---- bullets: most relevant first within each job (text unchanged)
    def bullet_rank(b: str) -> int:
        return -sum(1 for k in keywords if k["has"] and ((k["sid"] and COMPILED.get(k["sid"]) and COMPILED[k["sid"]].search(b))
                                                       or _literal(k["term"], b)))
    for e in cv["experience"]:
        e["bullets"] = sorted(e["bullets"], key=bullet_rank)

    after_text = _flat(cv)
    total = len(keywords) or 1
    before = sum(1 for k in keywords if _present(k["term"], base_text))
    after = sum(1 for k in keywords if _present(k["term"], after_text))
    report = {
        "keywords_total": len(keywords),
        "coverage_before": round(100 * before / total) if keywords else 100,
        "coverage_after": round(100 * after / total) if keywords else 100,
        "matched": [k["term"] for k in matched],
        "added": [k["term"] for k in added],
        "missing": [k["term"] for k in missing],
        "target": {"title": title, "company": job.get("company_display") or job.get("company") or ""},
    }
    return cv, report

"""Skill & responsibility lexicon used for deterministic extraction from job descriptions."""
from __future__ import annotations

import re

# id -> (regex, weight). Weight = how strongly the skill signals a BI/analytics role (affects importance).
SKILLS: dict[str, tuple[str, float]] = {
    "power_bi": (r"power\s?bi|pbi\b|power bi service|power bi desktop", 1.5),
    "dax": (r"\bdax\b", 1.2),
    "power_query": (r"power\s?query|\bm language\b", 1.0),
    "sql": (r"\bsql\b|t-sql|tsql|pl/sql|structured query language", 1.5),
    "sql_server": (r"sql server|mssql|ms sql", 0.8),
    "snowflake": (r"snowflake", 1.0),
    "excel": (r"\bexcel\b|spreadsheets?|pivot tables?|vlookup|xlookup", 0.8),
    "python": (r"\bpython\b", 1.0),
    "pandas": (r"\bpandas\b|numpy", 0.6),
    "azure": (r"\bazure\b(?! data factory)", 0.8),
    "microsoft_fabric": (r"microsoft fabric|\bms fabric\b|fabric (?:lakehouse|warehouse|data engineer)|onelake", 1.0),
    "azure_data_factory": (r"azure data factory|\badf\b|synapse", 0.8),
    "etl": (r"\betl\b|\belt\b|data pipelines?|data integration|extract,? transform", 1.0),
    "data_modeling": (r"data model(?:l)?ing|dimensional model|star schema|snowflake schema|semantic model|kimball", 1.0),
    "data_warehouse": (r"data warehous|\bdwh\b|data mart|lakehouse|data lake", 0.8),
    "kpi": (r"\bkpis?\b|key performance indicators?|metrics framework|scorecards?", 1.0),
    "dashboards": (r"dashboards?", 1.2),
    "reporting_automation": (r"automat\w* (?:of )?reports?|report(?:ing)? automation|automated reporting|automate (?:manual )?(?:reporting|processes)", 1.0),
    "data_quality": (r"data quality|data validation|data integrity|data accuracy|reconciliation", 0.8),
    "data_governance": (r"data governance|data stewardship|master data|\bmdm\b|data catalog", 0.6),
    "predictive_analytics": (r"predictive (?:analytics|model)", 0.6),
    "forecasting": (r"forecast", 0.7),
    "statistics": (r"statistic|hypothesis test|a/b test", 0.6),
    "regression": (r"regression", 0.5),
    "salesforce": (r"salesforce|\bsfdc\b|crm analytics|tableau crm", 0.6),
    "dynamics_365": (r"dynamics 365|\bd365\b|dynamics crm", 0.5),
    "crm_analytics": (r"\bcrm\b", 0.4),
    "data_migration": (r"migrat", 0.4),
    "ci_cd": (r"ci/cd|continuous integration|gitlab|azure devops|github actions", 0.4),
    "git": (r"\bgit\b|version control", 0.3),
    "agile": (r"\bagile\b|scrum|kanban|jira", 0.3),
    "stakeholder_management": (r"stakeholders?", 0.7),
    "requirements_gathering": (r"requirements? (?:gathering|elicitation|analysis)|gather (?:business )?requirements|user stories|\bbrd\b", 0.6),
    "business_analysis": (r"business analysis|process mapping|gap analysis|\bbpmn\b", 0.5),
    "financial_data": (r"financial data|market data|fund data|portfolio data|investment data", 0.6),
    "asset_management": (r"asset management|investment management|\baum\b|portfolio management|wealth management|funds?\b", 0.5),
    "sales_analytics": (r"sales (?:analytics|performance|operations|pipeline|reporting)|commercial analytics|revenue analytics", 0.6),
    "data_visualization": (r"data visuali[sz]ation|visuali[sz]e data|storytelling", 0.8),
    "cloud_data": (r"cloud data|cloud platform|cloud migration", 0.4),
    "tableau": (r"tableau(?! crm)", 1.0),
    "looker": (r"looker", 0.8),
    "qlik": (r"qlik", 0.8),
    "ssrs": (r"\bssrs\b|reporting services", 0.5),
    "ssis": (r"\bssis\b|integration services", 0.5),
    "databricks": (r"databricks", 0.7),
    "spark": (r"\bspark\b|pyspark|hadoop|hive\b", 0.7),
    "bigquery": (r"bigquery|big query", 0.7),
    "redshift": (r"redshift", 0.6),
    "aws": (r"\baws\b|amazon web services|\bs3\b|athena|glue\b", 0.6),
    "gcp": (r"\bgcp\b|google cloud", 0.6),
    "dbt": (r"\bdbt\b", 0.6),
    "airflow": (r"airflow", 0.5),
    "vba": (r"\bvba\b|macros", 0.4),
    "r_lang": (r"\br\b(?: programming| language|,? python| studio)|\brstudio\b|\bin r\b", 0.5),
    "sas": (r"\bsas\b", 0.5),
    "machine_learning": (r"machine learning|\bml\b|scikit|predictive model", 0.7),
    "deep_learning": (r"deep learning|tensorflow|pytorch|neural network|computer vision", 0.8),
    "nlp": (r"\bnlp\b|natural language processing|\bllms?\b|generative ai", 0.5),
    "api_integration": (r"\bapis?\b|rest api", 0.3),
    "sap": (r"\bsap\b|s/4hana|sap bw|sap bo", 0.5),
    "alteryx": (r"alteryx", 0.5),
    "financial_modeling": (r"financial model", 0.6),
    "fpa": (r"fp&a|financial planning (?:and|&) analysis|budgeting", 0.6),
    "accounting": (r"\bifrs\b|\bgaap\b|general ledger|accounts payable|accounts receivable|bookkeeping|month[- ]end close", 0.7),
    "java": (r"\bjava\b(?!script)", 0.6),
    "javascript": (r"javascript|typescript|react\b|node\.js|angular", 0.6),
    "scala": (r"\bscala\b", 0.6),
    "kubernetes": (r"kubernetes|docker|terraform", 0.4),
    "quant_finance": (r"quantitative (?:research|risk|finance|model|analyst)|derivatives|options pricing|stochastic|value at risk|greeks|monte carlo|margin methodology|backtest|pricing models?", 1.0),
    "ml_engineering": (r"mlops|model deployment|deploy(?:ing)? (?:ml|machine learning) models|feature store|recommendation systems?|personali[sz]ation models", 0.8),
    "arabic": (r"\barabic\b", 0.0),  # handled by eligibility, not technical match
}
COMPILED = {k: re.compile(v[0], re.I) for k, v in SKILLS.items()}

# Responsibilities the candidate HAS done (from CV) vs. ones outside the profile.
RESP_POSITIVE = {
    "build dashboards/reports": r"(?:build|develop|design|create|maintain)\w*\s+(?:\w+\s+){0,4}(?:dashboards?|reports?|visuali[sz]ations?)",
    "BI/reporting ownership": r"business intelligence|\bmis\b|management information|reporting (?:suite|framework|solutions?)|regular reporting",
    "KPI tracking": r"\bkpis?\b|performance (?:metrics|indicators|tracking|reporting)",
    "data modelling": r"data model|semantic layer|star schema|dimensional",
    "ETL/pipelines": r"\betl\b|\belt\b|pipelines?|data integration|data flows",
    "SQL querying & analysis": r"(?:write|writing|develop)\w*\s+(?:complex\s+)?(?:sql|queries)|query (?:data|databases)|sql queries",
    "ad-hoc analysis & insights": r"ad[- ]hoc|insights?|deep[- ]dives?|root cause|trend analysis",
    "stakeholder requirements": r"stakeholders?|business (?:users|partners|teams)|requirements",
    "automation": r"automat",
    "forecasting/predictive": r"forecast|predictive|regression",
    "data quality/governance": r"data quality|data integrity|validation|governance|reconcil",
    "migration/cloud": r"migrat|cloud",
    "executive reporting": r"senior management|leadership|executive|c-suite|board",
    "sales/commercial analytics": r"sales|commercial|revenue|pricing|customer (?:analytics|insights|retention)",
    "financial/investment data": r"financial|investment|portfolio|fund|asset management|risk reporting",
    "process improvement": r"process improvement|optimi[sz]",
}
RESP_NEGATIVE = {
    "software development": r"(?:develop|build)\w*\s+(?:\w+\s+){0,3}(?:applications?|microservices|mobile apps?|front[- ]?end|back[- ]?end|web apps?)|full[- ]stack|c\+\+|backend development",
    "people management": r"manage (?:a )?team of \d+|line manage|direct reports|lead a team of \d+|managing teams|lead and develop a team|people management|frontline|picking (?:operation|team)|picker",
    "sales / business development": r"sales targets?|quota|cold[- ]call|business development|new clients|lead gen|prospecting|hunter mentality|upsell|cross-sell(?:ing)? accounts|commercial negotiations|signing new businesses|merchant outreach",
    "deep ML research": r"deep learning|computer vision|research scientist|publish(?:ed)? papers|phd|transformer(?:-based)? architectures|graph-based retrieval",
    "accounting / financial statements": r"general ledger|accounts payable|accounts receivable|bookkeeping|month[- ]end close|audit fieldwork|\bifrs\b|consolidation process|financial statements|accounting standards",
    "compliance / AML": r"\baml\b|anti-money laundering|\bmlro\b|sanctions compliance|regulatory compliance program|kyc operations",
    "infrastructure/devops": r"kubernetes|on-call|site reliability|infrastructure as code|network engineer",
    "quant/trading research": r"derivatives pricing|trading experience|market microstructure|margin methodology|quantitative research|order book|delta one",
    "HR operations": r"onboarding management|hr help-?desk|visa (?:applications|cancellations)|payroll|employee relations|benefits administration",
    "hands-on operations": r"warehouse operations|customer service calls|front desk|housekeeping|cashier|on the ground|site visits",
}
RESP_POS_RX = {k: re.compile(v, re.I) for k, v in RESP_POSITIVE.items()}
RESP_NEG_RX = {k: re.compile(v, re.I) for k, v in RESP_NEGATIVE.items()}

HUMAN = {
    "power_bi": "Power BI", "dax": "DAX", "power_query": "Power Query", "sql": "SQL", "sql_server": "SQL Server",
    "snowflake": "Snowflake", "excel": "Excel", "python": "Python", "pandas": "pandas", "azure": "Azure",
    "microsoft_fabric": "Microsoft Fabric", "azure_data_factory": "Azure Data Factory/Synapse", "etl": "ETL/ELT",
    "data_modeling": "data modelling", "data_warehouse": "data warehousing", "kpi": "KPI frameworks",
    "dashboards": "dashboards", "reporting_automation": "reporting automation", "data_quality": "data quality",
    "data_governance": "data governance", "predictive_analytics": "predictive analytics", "forecasting": "forecasting",
    "statistics": "statistics/A-B testing", "regression": "regression", "salesforce": "Salesforce",
    "dynamics_365": "Dynamics 365", "crm_analytics": "CRM", "data_migration": "data migration", "ci_cd": "CI/CD",
    "git": "Git", "agile": "Agile/Scrum", "stakeholder_management": "stakeholder management",
    "requirements_gathering": "requirements gathering", "business_analysis": "business analysis",
    "financial_data": "financial data", "asset_management": "asset/investment management",
    "sales_analytics": "sales/commercial analytics", "data_visualization": "data visualisation",
    "cloud_data": "cloud data platforms", "tableau": "Tableau", "looker": "Looker", "qlik": "Qlik", "ssrs": "SSRS",
    "ssis": "SSIS", "databricks": "Databricks", "spark": "Spark/Hadoop", "bigquery": "BigQuery", "redshift": "Redshift",
    "aws": "AWS", "gcp": "Google Cloud", "dbt": "dbt", "airflow": "Airflow", "vba": "VBA/macros", "r_lang": "R",
    "sas": "SAS", "machine_learning": "machine learning", "deep_learning": "deep learning", "nlp": "NLP/LLMs",
    "api_integration": "APIs", "sap": "SAP", "alteryx": "Alteryx", "financial_modeling": "financial modelling",
    "fpa": "FP&A/budgeting", "accounting": "accounting (IFRS/GL)", "java": "Java", "javascript": "JavaScript/web",
    "scala": "Scala", "quant_finance": "quantitative finance/risk models", "ml_engineering": "ML engineering/MLOps", "kubernetes": "Docker/Kubernetes/Terraform", "arabic": "Arabic",
}

PREFERRED_MARKERS = re.compile(r"nice to have|preferred|desirable|a plus|an advantage|advantageous|bonus|good to have|ideally|beneficial", re.I)
REQUIRED_MARKERS = re.compile(r"must|required|requirements|essential|mandatory|you have|you bring|qualifications|minimum|proficien|strong|expert", re.I)


def skill_counts(text: str) -> dict[str, int]:
    out = {}
    for sid, rx in COMPILED.items():
        n = len(rx.findall(text or ""))
        if n:
            out[sid] = n
    return out


def extract_skills(text: str) -> dict[str, str]:
    """Return {skill_id: 'required'|'preferred'} for skills mentioned in the text (sentence-level context)."""
    found: dict[str, str] = {}
    present = {sid: rx for sid, rx in COMPILED.items() if rx.search(text or "")}  # cheap pre-filter
    if not present:
        return found
    for sent in re.split(r"(?<=[.;!?])\s+|\n+|•", text or ""):
        if not sent.strip():
            continue
        pref = bool(PREFERRED_MARKERS.search(sent))
        for sid, rx in present.items():
            if rx.search(sent):
                level = "preferred" if pref else "required"
                if found.get(sid) != "required":
                    found[sid] = level
    return found

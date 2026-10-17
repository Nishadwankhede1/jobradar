"""Build config/companies_eu.json for the Europe instance (JOBRADAR_REGION=eu) – Netherlands and UK employers.

1. Global employers already verified for the UAE list whose ATS feed covers every country (Workday, Greenhouse,
   Lever, Ashby, SmartRecruiters, Workable, Recruitee) – the adapters keep only jobs in the enabled countries.
2. International / English-speaking employers with a large Netherlands presence, read through the 'auto' adapter
   (it discovers their public ATS feed itself; nothing is assumed about whether they sponsor – the IND register and
   the job ad decide that).
3. Cross-employer job searches (Workable job search, Arbeitnow, Jooble when a key is set).
Run:  python scripts/build_companies_eu.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLOBAL_ATS = {"workday", "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"}

# Employers with a sizeable Amsterdam / Netherlands office that work in English (tech, fintech, trading, consumer).
# slugs = hints for the ATS feed name when it differs from the company name.
NL_EMPLOYERS = [
    ("Adyen", ["adyen"], "Fintech"), ("Booking.com", ["bookingcom", "booking"], "Travel tech"),
    ("Mollie", ["mollie"], "Fintech"), ("bunq", ["bunq"], "Fintech"), ("Bitvavo", ["bitvavo"], "Fintech"),
    ("Picnic", ["picnic", "picnictechnologies"], "E-commerce"), ("Miro", ["realtimeboardglobal", "miro"], "Software"),
    ("Elastic", ["elastic"], "Software", ["elasticsearch B.V."]), ("Databricks", ["databricks"], "Software"), ("Uber", ["uber"], "Mobility"),
    ("Netflix", ["netflix"], "Media"), ("Optiver", ["optiver"], "Trading"), ("IMC Trading", ["imc", "imctrading"], "Trading"),
    ("Flow Traders", ["flowtraders"], "Trading"), ("Da Vinci Trading", ["davincitrading", "davinci"], "Trading"),
    ("Catawiki", ["catawiki"], "E-commerce"), ("Bird", ["messagebird", "bird"], "Software"),
    ("Just Eat Takeaway.com", ["justeattakeaway", "takeaway"], "E-commerce"), ("bol.com", ["bol", "bolcom"], "E-commerce"),
    ("Backbase", ["backbase"], "Fintech"), ("WeTransfer", ["wetransfer"], "Software"), ("TomTom", ["tomtom"], "Software"),
    ("Sendcloud", ["sendcloud"], "Software"), ("Framer", ["framer"], "Software"), ("Mews", ["mews"], "Software"),
    ("Trengo", ["trengo"], "Software"), ("Lightyear", ["lightyear"], "Fintech"), ("Bux", ["bux"], "Fintech"),
    ("Ohpen", ["ohpen"], "Fintech"), ("Tiqets", ["tiqets"], "Travel tech"), ("GetYourGuide", ["getyourguide"], "Travel tech"),
    ("Mambu", ["mambu"], "Fintech"), ("Personio", ["personio"], "Software"), ("Remote", ["remote", "remotecom"], "Software"),
    ("Rituals", ["rituals"], "Consumer"), ("Swapfiets", ["swapfiets"], "Mobility"), ("Fastned", ["fastned"], "Energy"),
    ("Vinted", ["vinted"], "E-commerce"), ("Tesla", ["tesla"], "Automotive"), ("Glovo", ["glovo"], "E-commerce"),
    ("Coolblue", ["coolblue"], "E-commerce"), ("Effectory", ["effectory"], "Software"), ("Channable", ["channable"], "Software"),
    ("Productboard", ["productboard"], "Software"), ("Payvision", ["payvision"], "Fintech"), ("Klarna", ["klarna"], "Fintech"),
    ("Spotify", ["spotify"], "Media"), ("Stripe", ["stripe"], "Fintech"), ("Zalando", ["zalando"], "E-commerce"),
    ("Fyber", ["fyber", "digitalturbine"], "Ad tech"), ("Taxfix", ["taxfix"], "Fintech"), ("Hiber", ["hiber"], "IoT"),
    ("Polarsteps", ["polarsteps"], "Travel tech"), ("Otrium", ["otrium"], "E-commerce"), ("Tellent", ["tellent", "recruitee"], "Software"),
    ("Ecosia", ["ecosia"], "Software"), ("Bynder", ["bynder"], "Software"), ("Mendix", ["mendix"], "Software"),
    ("Pon", ["pon"], "Mobility"), ("Exact", ["exact"], "Software"), ("AFAS Software", ["afas"], "Software"),
    ("Ahold Delhaize", ["aholddelhaize"], "Retail"), ("Philips", ["philips"], "Health tech"), ("ASML", ["asml"], "Semiconductors"),
    ("Heineken", ["heineken"], "Consumer"), ("Prosus", ["prosus", "naspers"], "Tech investing"),
    ("Wolters Kluwer", ["wolterskluwer"], "Information services"), ("Elsevier", ["elsevier", "relx"], "Information services"),
    ("Nike", ["nike"], "Consumer"), ("PVH", ["pvh"], "Consumer"), ("Signify", ["signify"], "Electronics"),
    ("ING", ["ing"], "Banking"), ("ABN AMRO", ["abnamro"], "Banking"), ("Rabobank", ["rabobank"], "Banking"),
    ("NN Group", ["nngroup", "nn"], "Insurance"), ("Aegon", ["aegon"], "Insurance"), ("Randstad", ["randstad"], "HR services"),
]

# Employers with a large London / UK data team (fintech, consumer tech, trading, data). Same rules: the Home Office
# register and the job ad decide whether a job can be sponsored.
UK_EMPLOYERS = [
    ("Revolut", ["revolut"], "Fintech"), ("Monzo", ["monzo"], "Fintech"), ("Wise", ["wise", "transferwise"], "Fintech"),
    ("Starling Bank", ["starlingbank", "starling"], "Fintech"), ("Checkout.com", ["checkoutcom", "checkout"], "Fintech"),
    ("GoCardless", ["gocardless"], "Fintech"), ("Zopa", ["zopa"], "Fintech"), ("OakNorth", ["oaknorth"], "Fintech"),
    ("Moneybox", ["moneybox"], "Fintech"), ("Tide", ["tide"], "Fintech"), ("Cleo", ["cleo", "cleoai"], "Fintech"),
    ("ClearScore", ["clearscore"], "Fintech"), ("Funding Circle", ["fundingcircle"], "Fintech"), ("Marshmallow", ["marshmallow"], "Insurtech"),
    ("Thought Machine", ["thoughtmachine"], "Fintech"), ("Paddle", ["paddle"], "Fintech"), ("Curve", ["curve"], "Fintech"),
    ("Lendable", ["lendable"], "Fintech"), ("Nutmeg", ["nutmeg"], "Fintech"), ("Freetrade", ["freetrade"], "Fintech"),
    ("Deliveroo", ["deliveroo"], "E-commerce"), ("Ocado Technology", ["ocado", "ocadotechnology"], "E-commerce"),
    ("Octopus Energy", ["octopusenergy", "octopus"], "Energy"), ("Gousto", ["gousto"], "E-commerce"), ("ASOS", ["asos"], "E-commerce"),
    ("Farfetch", ["farfetch"], "E-commerce"), ("Trainline", ["trainline"], "Travel tech"), ("Skyscanner", ["skyscanner"], "Travel tech"),
    ("Snyk", ["snyk"], "Software"), ("Darktrace", ["darktrace"], "Software"), ("Onfido", ["onfido"], "Software"),
    ("Babbel", ["babbel"], "Software"), ("Multiverse", ["multiverse"], "Edtech"), ("Zego", ["zego"], "Insurtech"),
    ("Cazoo", ["cazoo"], "E-commerce"), ("Bumble", ["bumble"], "Consumer tech"), ("Depop", ["depop"], "E-commerce"),
    ("Monese", ["monese"], "Fintech"), ("Ebury", ["ebury"], "Fintech"), ("Kraken Technologies", ["kraken", "krakentechnologies"], "Energy"),
    ("Palantir", ["palantir"], "Software"), ("Bloomberg", ["bloomberg"], "Financial data"), ("Moody's", ["moodys"], "Financial data"),
    ("LSEG", ["lseg"], "Financial data"), ("Hargreaves Lansdown", ["hargreaveslansdown"], "Asset management"),
    ("Schroders", ["schroders"], "Asset management"), ("abrdn", ["abrdn"], "Asset management"), ("M&G", ["mandg"], "Asset management"),
    ("Man Group", ["mangroup"], "Asset management"), ("Marshall Wace", ["marshallwace"], "Asset management"),
    ("G-Research", ["gresearch"], "Trading"), ("Jane Street", ["janestreet"], "Trading"), ("Citadel", ["citadel"], "Trading"),
    ("Flutter UK & Ireland", ["flutter"], "Gaming"), ("Sky Betting & Gaming", ["skybettingandgaming"], "Gaming"),
    ("Arm", ["arm"], "Semiconductors"), ("Sainsbury's Tech", ["sainsburys"], "Retail"), ("Tesco Technology", ["tesco"], "Retail"),
    ("BBC", ["bbc"], "Media"), ("Channel 4", ["channel4"], "Media"), ("Just Eat", ["justeat"], "E-commerce"),
]

SEARCH_COUNTRY = {"nl": "Netherlands", "uk": "United Kingdom", "de": "Germany"}


def enabled_countries():
    import tomllib
    with open(ROOT / "config" / "regions" / "eu.toml", "rb") as f:
        return (tomllib.load(f).get("region") or {}).get("countries") or ["nl"]


SEARCHES = [
    {"id": "workable-search-eu", "name": "Workable job search (Netherlands)", "ats": "workable_search", "kind": "aggregator",
     "priority": 2, "industry": "", "ats_config": {"location": "Netherlands", "queries": [
         "data analyst", "business intelligence", "power bi", "bi analyst", "analytics engineer", "reporting analyst",
         "data engineer", "crm analyst", "insights analyst", "sql analyst"]}},
    {"id": "arbeitnow", "name": "Arbeitnow (DE / NL job board)", "ats": "arbeitnow", "kind": "aggregator", "priority": 2,
     "industry": "", "ats_config": {"pages": 5}},
    {"id": "jooble-eu", "name": "Jooble (Netherlands)", "ats": "jooble", "kind": "aggregator", "priority": 3, "industry": "",
     "ats_config": {"location": "Netherlands", "queries": ["data analyst english", "business intelligence analyst",
                                                           "power bi developer", "analytics engineer", "bi developer"]}},
]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    import tomllib
    uae = json.loads((ROOT / "config" / "companies.json").read_text(encoding="utf-8"))
    with open(ROOT / "config" / "settings.toml", "rb") as f:  # boards that can't be read automatically (see settings)
        unreadable = set((tomllib.load(f).get("sources") or {}).get("alerts_only") or [])
    out, seen = [], set()
    for c in uae:
        if c.get("ats") in GLOBAL_ATS | {"auto"} and c.get("kind", "employer") == "employer" and c.get("id") not in unreadable:
            e = {k: c[k] for k in ("id", "name", "industry", "website", "careers_url", "ats", "ats_config") if k in c}
            e.update({"priority": 2, "kind": "employer", "verified": bool(c.get("verified")),
                      "verification_note": "Global ATS feed already verified for the UAE list – filtered to the enabled countries",
                      "discovered_in": "companies.json (global feed)"})
            key = re.sub(r"[^a-z0-9]", "", c["name"].lower())
            alias = next((a[0] for n, _, _, *a in NL_EMPLOYERS if a and re.sub(r"[^a-z0-9]", "", n.lower()) == key), None)
            if alias:
                e["ats_config"] = {**e.get("ats_config", {}), "sponsor_names": alias}
            out.append(e)
            seen.add(key)
    for name, slugs, ind, *alias in NL_EMPLOYERS:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in seen:
            continue
        out.append({"id": "nl-" + slug(name), "name": name, "industry": ind, "ats": "auto", "ats_config": {"slugs": slugs, **({"sponsor_names": alias[0]} if alias else {})},
                    "priority": 1, "kind": "employer", "verified": False,
                    "verification_note": "Public ATS feed found automatically on first scan (re-checked weekly if none)",
                    "discovered_in": "build_companies_eu.py (NL employers)"})
        seen.add(key)
    import os
    countries = [c.strip() for c in (os.environ.get("JOBRADAR_COUNTRIES") or "").split(",") if c.strip()] or enabled_countries()
    if "uk" in countries:
        for name, slugs, ind, *alias in UK_EMPLOYERS:
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key in seen:
                continue
            out.append({"id": "uk-" + slug(name), "name": name, "industry": ind, "ats": "auto",
                        "ats_config": {"slugs": slugs, **({"sponsor_names": alias[0]} if alias else {})},
                        "priority": 1, "kind": "employer", "verified": False,
                        "verification_note": "Public ATS feed found automatically on first scan (re-checked weekly if none)",
                        "discovered_in": "build_companies_eu.py (UK employers)"})
            seen.add(key)
    for c in countries:  # one Workable / Jooble search per country ("workable-search-eu" keeps its id for the NL one)
        if c not in SEARCH_COUNTRY:
            continue
        for base in SEARCHES:
            if base["ats"] == "arbeitnow":
                if c == countries[0]:
                    out.append(base)
                continue
            e = json.loads(json.dumps(base))
            e["ats_config"]["location"] = SEARCH_COUNTRY[c]
            e["name"] = re.sub(r"\(.*\)", f"({SEARCH_COUNTRY[c]})", e["name"])
            if c != "nl":
                e["id"] = f"{base['id']}-{c}"
            out.append(e)
    (ROOT / "config" / "companies_eu.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(out)} sources written: {sum(1 for x in out if x['ats'] == 'auto')} auto-discovered employers, "
          f"{sum(1 for x in out if x.get('discovered_in', '').startswith('companies.json'))} global feeds, "
          f"{sum(1 for x in out if x.get('kind') == 'aggregator')} searches ({', '.join(countries)})")


if __name__ == "__main__":
    main()

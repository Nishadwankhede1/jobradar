"""Location understanding: UAE / Dubai (default) or Netherlands / Germany / UK (JOBRADAR_REGION=eu)."""
from __future__ import annotations

import re

UAE_RX = re.compile(r"\b(united arab emirates|u\.a\.e\.?|uae|"
                    r"dubai|abu dhabi|sharjah|ajman|ras al[- ]khaimah|fujairah|umm al[- ]quwain|al ain|difc|dmcc|"
                    r"jebel ali|jafza|dubai internet city|dubai media city|business bay|"
                    r"dubai silicon oasis|masdar|adgm)\b", re.I)
DUBAI_RX = re.compile(r"\b(dubai|difc|dmcc|jebel ali|jafza|business bay|internet city|media city|silicon oasis|"
                      r"dubai south|deira|bur dubai|al quoz|jlt|jumeirah|marina)\b", re.I)
ABU_DHABI_RX = re.compile(r"\b(abu dhabi|al ain|adgm|masdar|ruwais|saadiyat|yas island|al maryah|reem island|etihad towers)\b", re.I)
#: brand/district words that also occur in Abu Dhabi property names ("Jumeirah Saadiyat Island", "Marina Mall Abu Dhabi")
WEAK_DUBAI = {"jumeirah", "marina"}
NORTHERN_RX = re.compile(r"\b(sharjah|ajman|ras al[- ]khaimah|rak\b|fujairah|umm al[- ]quwain)\b", re.I)
REMOTE_RX = re.compile(r"\b(remote|work from home|anywhere)\b", re.I)
UAE_CODES = {"AE", "ARE", "UAE"}


def classify(location: str, country_code: str | None = None, description: str = "") -> dict:
    """Return {'in_uae', 'city', 'tier'} where tier is: dubai | uae_other | uae_unspecified | remote | outside.

    In the EU instance 'in_uae' means "in one of the enabled countries" and tier is nl | de | uk | remote | outside."""
    from .region import is_eu
    if is_eu():
        return classify_eu(location, country_code, description)
    loc = location or ""
    cc = (country_code or "").upper()
    in_uae = bool(UAE_RX.search(loc)) or cc in UAE_CODES
    dubai_hits = {m.group(1).lower() for m in DUBAI_RX.finditer(loc)}
    if dubai_hits - WEAK_DUBAI:
        return {"in_uae": True, "city": "Dubai", "tier": "dubai"}
    if ABU_DHABI_RX.search(loc):
        return {"in_uae": True, "city": "Abu Dhabi", "tier": "uae_other"}
    if dubai_hits:
        return {"in_uae": True, "city": "Dubai", "tier": "dubai"}
    if NORTHERN_RX.search(loc):
        m = NORTHERN_RX.search(loc)
        return {"in_uae": True, "city": m.group(1).title(), "tier": "uae_other"}
    if in_uae:
        # country only – see if the description pins it to Dubai
        if DUBAI_RX.search(description[:3000] if description else ""):
            return {"in_uae": True, "city": "Dubai", "tier": "dubai"}
        return {"in_uae": True, "city": "UAE", "tier": "uae_unspecified"}
    if REMOTE_RX.search(loc) and re.search(r"\b(uae|dubai|gcc|mena|middle east|emea)\b", loc + " " + (description or "")[:1500], re.I):
        return {"in_uae": False, "city": "Remote", "tier": "remote"}
    return {"in_uae": False, "city": loc.split(",")[0].strip() or "Unknown", "tier": "outside"}


def mentions_uae(text: str) -> bool:
    return bool(UAE_RX.search(text or ""))


# ------------------------------------------------------------------ Europe (JOBRADAR_REGION=eu)
EU_CITIES = {
    "nl": r"netherlands|nederland|holland|\bnl\b|amsterdam|rotterdam|the hague|den haag|'s-gravenhage|utrecht|eindhoven|groningen|"
          r"tilburg|almere|breda|nijmegen|haarlem|arnhem|amersfoort|leiden|delft|hilversum|amstelveen|hoofddorp|schiphol|"
          r"zwolle|maastricht|enschede|den bosch|'s-hertogenbosch|diemen|veldhoven|apeldoorn|zaandam|capelle|rijswijk|"
          r"zoetermeer|leeuwarden|dordrecht|deventer|venlo|helmond|naarden|woerden|nieuwegein|houten|zeist|wageningen",
    "de": r"germany|deutschland|berlin|munich|münchen|muenchen|hamburg|frankfurt|cologne|köln|koeln|stuttgart|"
          r"düsseldorf|dusseldorf|duesseldorf|leipzig|dresden|hannover|hanover|nuremberg|nürnberg|nuernberg|bonn|karlsruhe|"
          r"mannheim|essen|dortmund|bremen|heidelberg|wiesbaden|mainz|aachen|freiburg|potsdam|augsburg|walldorf|ingolstadt|"
          r"wolfsburg|erlangen|darmstadt|münster|muenster|bielefeld|bochum|duisburg|regensburg",
    "uk": r"united kingdom|\buk\b|\bu\.k\.|great britain|england|scotland|wales|northern ireland|london|manchester|"
          r"birmingham|edinburgh|glasgow|leeds|bristol|cambridge|oxford|belfast|cardiff|liverpool|newcastle|sheffield|"
          r"nottingham|reading|milton keynes|brighton|southampton|leicester|coventry|bath|york|aberdeen|guildford|"
          r"slough|watford|croydon|canary wharf",
}
EU_RX = {c: re.compile(rf"(?:{p})", re.I) for c, p in EU_CITIES.items()}
EU_CODES = {"NL": "nl", "NLD": "nl", "DE": "de", "DEU": "de", "GB": "uk", "GBR": "uk", "UK": "uk"}
# same city names elsewhere (Birmingham AL, Cambridge MA, London Ontario, Berlin NH, Hamburg NY, Delft…)
NOT_EU_RX = re.compile(r",\s*(?:al|ma|ny|nh|ct|nj|pa|ca|tx|on|ontario|ky|oh|mi|wi|mn|usa|us|united states|canada)\b|"
                       r"\b(?:usa|united states|canada|ontario|massachusetts|alabama|new york)\b", re.I)
EU_REMOTE_RX = re.compile(r"\b(europe|european union|\beu\b|emea|netherlands|germany|united kingdom|uk|cet|cest)\b", re.I)


def _eu_city(loc: str, rx: re.Pattern) -> str:
    m = rx.search(loc)
    if not m:
        return ""
    w = m.group(0).strip()
    if w.lower() in ("nl", "de", "uk", "u.k.", "netherlands", "nederland", "holland", "germany", "deutschland",
                     "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland"):
        return ""
    return {"den haag": "The Hague", "'s-gravenhage": "The Hague", "münchen": "Munich", "muenchen": "Munich",
            "köln": "Cologne", "koeln": "Cologne", "nürnberg": "Nuremberg", "nuernberg": "Nuremberg",
            "düsseldorf": "Düsseldorf", "dusseldorf": "Düsseldorf", "duesseldorf": "Düsseldorf",
            "'s-hertogenbosch": "Den Bosch", "hanover": "Hannover"}.get(w.lower(), w.title())


def classify_eu(location: str, country_code: str | None = None, description: str = "") -> dict:
    """Tier nl | de | uk for jobs in an enabled country, remote for Europe-wide remote roles, else outside."""
    from .region import COUNTRY_NAMES, enabled_countries
    loc = location or ""
    on = enabled_countries()
    cc = EU_CODES.get((country_code or "").upper())
    found = [c for c in ("nl", "de", "uk") if EU_RX[c].search(loc)]
    if NOT_EU_RX.search(loc) and not cc:
        found = []
    if cc and cc not in found:
        found.insert(0, cc)
    hit = next((c for c in found if c in on), None)
    if hit:
        return {"in_uae": True, "city": _eu_city(loc, EU_RX[hit]) or COUNTRY_NAMES[hit], "tier": hit, "country": hit}
    if found:  # a real place in NL/DE/UK, but that country isn't switched on yet
        return {"in_uae": False, "city": _eu_city(loc, EU_RX[found[0]]) or COUNTRY_NAMES[found[0]], "tier": "outside",
                "country": found[0]}
    if REMOTE_RX.search(loc) and EU_REMOTE_RX.search(loc + " " + (description or "")[:1500]):
        return {"in_uae": True, "city": "Remote (Europe)", "tier": "remote", "country": ""}
    return {"in_uae": False, "city": loc.split(",")[0].strip() or "Unknown", "tier": "outside", "country": ""}


def region_iso2() -> list[str]:
    """Lower-case ISO-2 codes of the countries this instance watches (for ATS 'country' filters)."""
    from .region import enabled_countries, is_eu
    return [{"uk": "gb"}.get(c, c) for c in enabled_countries()] if is_eu() else ["ae"]


def region_rx() -> re.Pattern:
    """Regex that recognises a place in the watched countries (Workday facets, quick pre-filters)."""
    from .region import enabled_countries, is_eu
    if not is_eu():
        return UAE_RX
    return re.compile("|".join(f"(?:{EU_CITIES[c]})" for c in enabled_countries() if c in EU_CITIES) or "(?!)", re.I)


def region_query() -> str:
    """Free-text location used where a site only offers a search box ('United Arab Emirates' / 'Netherlands')."""
    from .region import COUNTRY_NAMES, enabled_countries, is_eu
    return COUNTRY_NAMES.get(enabled_countries()[0], "Netherlands") if is_eu() else "United Arab Emirates"

"""Location understanding for UAE / Dubai vacancies."""
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
    """Return {'in_uae', 'city', 'tier'} where tier is: dubai | uae_other | uae_unspecified | remote | outside."""
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

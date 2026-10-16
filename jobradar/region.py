"""Which job market this JobRadar instance watches.

JOBRADAR_REGION=uae (default) – Dubai / UAE, the original setup.
JOBRADAR_REGION=eu            – Netherlands → Germany → UK, English-speaking employers that can sponsor a visa.
The EU instance uses the same code with its own settings overlay (config/regions/eu.toml), company list, database
and dashboard, so the UAE monitor is never slowed down or changed by it.
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

ROOT = Path(os.environ.get("JOBRADAR_HOME", Path(__file__).resolve().parent.parent))
REGION = (os.environ.get("JOBRADAR_REGION") or "uae").strip().lower()

COUNTRY_NAMES = {"nl": "Netherlands", "de": "Germany", "uk": "United Kingdom"}
COUNTRY_FLAGS = {"nl": "🇳🇱", "de": "🇩🇪", "uk": "🇬🇧"}


def is_eu() -> bool:
    return REGION == "eu"


@lru_cache(maxsize=1)
def overlay() -> dict:
    p = ROOT / "config" / "regions" / f"{REGION}.toml"
    if REGION == "uae" or not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


@lru_cache(maxsize=1)
def enabled_countries() -> tuple[str, ...]:
    """Countries switched on for the EU instance, in priority order (env JOBRADAR_COUNTRIES=nl,de overrides)."""
    env = os.environ.get("JOBRADAR_COUNTRIES")
    vals = env.split(",") if env else (overlay().get("region", {}).get("countries") or ["nl"])
    return tuple(c.strip().lower() for c in vals if c.strip())


def place_label() -> str:
    """'the UAE' / 'the Netherlands' – for messages."""
    if not is_eu():
        return "the UAE"
    names = [COUNTRY_NAMES.get(c, c.upper()) for c in enabled_countries()]
    names = [("the " + n) if n in ("Netherlands", "United Kingdom") else n for n in names]
    return ", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else (names[0] if names else "Europe")


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

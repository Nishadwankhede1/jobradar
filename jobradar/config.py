"""Configuration loading (TOML + JSON, stdlib only)."""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("JOBRADAR_HOME", Path(__file__).resolve().parent.parent))


def _load_toml(name: str) -> dict:
    with open(ROOT / "config" / name, "rb") as f:
        return tomllib.load(f)


def load_dotenv(path: Path | None = None):
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Taxonomy:
    families: list[dict]
    negative: list[re.Pattern]
    seniority: dict[str, float]
    queries: list[str]
    compiled: list[tuple[str, float, re.Pattern]] = field(default_factory=list)

    @classmethod
    def load(cls, data: dict) -> "Taxonomy":
        t = cls(families=data.get("family", []),
                negative=[re.compile(rf"\b{p}", re.I) for p in data.get("negative", {}).get("titles", [])],
                seniority=data.get("seniority", {}), queries=data.get("search_queries", {}).get("queries", []))
        for fam in t.families:
            for pat in fam.get("titles", []):
                t.compiled.append((fam["name"], float(fam.get("fit", 0.6)), re.compile(rf"\b{pat}", re.I)))
        return t

    def match_title(self, norm_title: str) -> tuple[str | None, float]:
        best = (None, 0.0)
        for fam, fit, rx in self.compiled:
            if rx.search(norm_title) and fit > best[1]:
                best = (fam, fit)
        return best

    def is_negative(self, norm_title: str) -> bool:
        return any(rx.search(norm_title) for rx in self.negative)


@dataclass
class Settings:
    raw: dict
    profile: dict
    taxonomy: Taxonomy

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    load_dotenv()
    raw = _load_toml("settings.toml")
    # env overrides for the knobs people tweak most
    for env, (sec, key, typ) in {"NOTIFY_THRESHOLD": ("scoring", "notify_threshold", float),
                                 "IMMEDIATE_THRESHOLD": ("scoring", "immediate_threshold", float),
                                 "LLM_PROVIDER": ("llm", "provider", str), "LLM_MODEL": ("llm", "model", str),
                                 "LLM_BASE_URL": ("llm", "base_url", str),
                                 "JOBRADAR_DB": ("paths", "database", str),
                                 "JOBRADAR_MAX_RUNTIME": ("scan", "max_runtime_seconds", int),
                                 "JOBRADAR_WORKERS": ("scan", "workers", int)}.items():
        if os.environ.get(env):
            raw.setdefault(sec, {})[key] = typ(os.environ[env])
    return Settings(raw=raw, profile=_load_toml("profile.toml"), taxonomy=Taxonomy.load(_load_toml("taxonomy.toml")))


def load_companies(settings: Settings) -> list[dict]:
    with open(settings.path("companies"), encoding="utf-8") as f:
        companies = json.load(f)
    # employers whose sites block or disallow automated reading (e.g. from cloud servers) → follow them via job alerts
    alerts_only = set(settings.get("sources", "alerts_only", []) or [])
    for c in companies:
        if c.get("id") in alerts_only and c.get("ats") not in ("manual", "email"):
            c["ats"] = "manual"
            c["verification_note"] = ("Site blocks or disallows automated reading from the cloud – followed through "
                                      "LinkedIn / e-mail job alerts instead")
    return companies

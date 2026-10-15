"""Adapter contract. Each ATS integration implements `fetch(company, ctx) -> AdapterResult`.

New platforms are added by subclassing `Adapter` and decorating with `@register("name")` — nothing else changes.
"""
from __future__ import annotations

from ..http import HttpClient
from ..location import classify
from ..models import AdapterResult, FetchContext, RawJob

REGISTRY: dict[str, type["Adapter"]] = {}


def register(name: str):
    def deco(cls):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


class AdapterConfigError(Exception):
    pass


class Adapter:
    name = "base"
    #: whether this adapter scrapes HTML pages (robots.txt is checked) vs official JSON APIs/feeds
    html_scraper = False

    def __init__(self, http: HttpClient):
        self.http = http

    def fetch(self, company: dict, ctx: FetchContext) -> AdapterResult:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------ helpers
    @staticmethod
    def cfg(company: dict, key: str, default=None, required=True):
        v = (company.get("ats_config") or {}).get(key, default)
        if required and v in (None, ""):
            raise AdapterConfigError(f"{company.get('name')}: ats_config.{key} missing")
        return v

    @staticmethod
    def is_uae(location: str, country_code: str | None = None, description: str = "") -> bool:
        return classify(location, country_code, description)["in_uae"]

    @staticmethod
    def need_detail(ctx: FetchContext, external_id: str, marker: str | None) -> bool:
        if external_id not in ctx.known:
            return True
        old = ctx.known.get(external_id) or ""
        return bool(marker) and marker != old


def get_adapter(name: str, http: HttpClient) -> Adapter:
    if name not in REGISTRY:
        raise AdapterConfigError(f"no adapter registered for ats '{name}'")
    return REGISTRY[name](http)


__all__ = ["Adapter", "AdapterResult", "AdapterConfigError", "FetchContext", "RawJob", "register", "get_adapter", "REGISTRY"]

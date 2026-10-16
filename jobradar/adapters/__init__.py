"""Importing this package registers every built-in adapter."""
from . import ats_json, auto, browser, enterprise, html_sites, web, workday  # noqa: F401
from .base import REGISTRY, get_adapter  # noqa: F401

"""Core data structures shared by adapters, pipeline and scoring."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawJob:
    """A vacancy exactly as an adapter found it (before normalisation / scoring)."""
    source: str                       # adapter name, e.g. "greenhouse", "email:linkedin"
    external_id: str                  # id unique within (company, source)
    title: str
    url: str                          # best application/detail URL (official where possible)
    location: str = ""
    description: str | None = None    # plain text. None = "not fetched this run, keep stored copy"
    employment_type: str | None = None
    posted_at: str | None = None      # ISO timestamp if the source publishes one
    department: str | None = None
    company_name: str | None = None   # override for multi-brand boards / aggregators
    country_code: str | None = None   # ISO-2 if the source provides it
    updated_marker: str | None = None  # list-level "updated_at" used to decide detail refetch
    is_official: bool = True          # False for job-board / email-alert leads
    is_agency: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class FetchContext:
    """Information the pipeline passes to an adapter for incremental fetching."""
    known: dict = field(default_factory=dict)   # external_id -> updated_marker (or "" if none)
    max_detail_fetches: int = 60
    max_pages: int = 25
    state: dict = field(default_factory=dict)    # per-source scratch state persisted between runs (e.g. page cache)


@dataclass
class AdapterResult:
    jobs: list[RawJob]
    complete: bool = True     # True when the adapter saw the full UAE listing (safe to infer closures)
    not_modified: bool = False
    notes: str = ""
    state_out: dict | None = None  # new per-source state to persist (None = leave unchanged)
    baseline_ready: bool = True  # False while a progressive crawler has not yet seen the whole listing once

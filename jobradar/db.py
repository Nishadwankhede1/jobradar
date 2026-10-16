"""SQLite persistence: companies, monitored sources, jobs, events, scores, notifications, scan runs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT, website TEXT, careers_url TEXT,
  ats TEXT, ats_config TEXT, dubai_presence TEXT, job_families TEXT, priority INTEGER DEFAULT 3,
  verified INTEGER DEFAULT 0, verification_note TEXT, hires_evidence TEXT,
  kind TEXT DEFAULT 'employer',            -- employer | aggregator | lead (auto-discovered from a job lead)
  enabled INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  company_id TEXT PRIMARY KEY REFERENCES companies(id),
  interval_minutes INTEGER, next_due_at TEXT, last_run_at TEXT, last_success_at TEXT,
  last_status TEXT, last_error TEXT, consecutive_failures INTEGER DEFAULT 0,
  last_job_count INTEGER, total_runs INTEGER DEFAULT 0, total_failures INTEGER DEFAULT 0,
  last_change_at TEXT, baseline_done INTEGER DEFAULT 0, last_duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id TEXT, company_display TEXT, source TEXT, external_id TEXT,
  title TEXT, norm_title TEXT, url TEXT, location TEXT, city TEXT, location_tier TEXT,
  description TEXT, employment_type TEXT, department TEXT, posted_at TEXT,
  first_seen_at TEXT, last_seen_at TEXT, last_changed_at TEXT, closed_at TEXT,
  status TEXT DEFAULT 'active', missing_count INTEGER DEFAULT 0,
  content_hash TEXT, updated_marker TEXT, fingerprint TEXT,
  duplicate_of INTEGER, reposted_from INTEGER,
  is_official INTEGER DEFAULT 1, is_agency INTEGER DEFAULT 0, verification TEXT, evidence TEXT,
  latency_minutes REAL, baseline INTEGER DEFAULT 0, extra TEXT,
  UNIQUE(company_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS scores (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id), content_hash TEXT, score REAL, label TEXT,
  stage_a_pass INTEGER, stage_a_reason TEXT, hard_disqualifiers TEXT, subscores TEXT,
  matched_skills TEXT, missing_skills TEXT, reasons TEXT, gaps TEXT, uncertainties TEXT,
  recommendation TEXT, confidence TEXT, method TEXT, scored_at TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, event TEXT, at TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, channel TEXT, kind TEXT, content_hash TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, sent_at TEXT, error TEXT, message_ref TEXT,
  UNIQUE(job_id, channel, kind, content_hash)
);
CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, digest_date TEXT, channel TEXT, status TEXT, sent_at TEXT,
  job_count INTEGER, error TEXT, UNIQUE(digest_date, channel)
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, trigger TEXT,
  sources_checked INTEGER, sources_ok INTEGER, sources_failed INTEGER, sources_not_modified INTEGER,
  jobs_seen INTEGER, jobs_new INTEGER, jobs_updated INTEGER, jobs_closed INTEGER,
  alerts_sent INTEGER, alerts_failed INTEGER, http_requests INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS email_seen (message_id TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (content_hash TEXT PRIMARY KEY, result TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applications (
  job_id INTEGER PRIMARY KEY, company_id TEXT, company TEXT, title TEXT, norm_title TEXT, fingerprint TEXT, url TEXT,
  city TEXT, score REAL, cv_file TEXT, status TEXT DEFAULT 'applied', note TEXT, applied_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS resumes (
  job_id INTEGER PRIMARY KEY, content_hash TEXT, file TEXT, coverage_before INTEGER, coverage_after INTEGER,
  keywords_total INTEGER, matched TEXT, added TEXT, missing TEXT, created_at TEXT
);
"""

JSON_COLS = {"ats_config", "job_families", "hard_disqualifiers", "subscores", "matched_skills", "missing_skills",
             "reasons", "gaps", "uncertainties", "extra", "visa", "cv_added", "cv_missing", "cv_matched"}


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str) and d[k]:
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


class Database:
    def __init__(self, path: str | Path = "state/jobradar.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")  # single-file DB → safe to commit to git
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.executescript(SCHEMA)
        if "visa" not in {r[1] for r in self.conn.execute("PRAGMA table_info(scores)")}:
            self.conn.execute("ALTER TABLE scores ADD COLUMN visa TEXT")  # Europe instance: sponsor / language badges
        self.conn.commit()

    # ---------------------------------------------------------------- generic
    def q(self, sql: str, args=()) -> list[dict]:
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        return _row(self.conn.execute(sql, args).fetchone())

    def scalar(self, sql: str, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else None

    def execute(self, sql: str, args=()):
        cur = self.conn.execute(sql, args)
        return cur

    def commit(self):
        self.conn.commit()

    def upsert(self, table: str, data: dict, key: list[str]):
        data = {k: _enc(v) for k, v in data.items()}
        cols = ", ".join(data)
        ph = ", ".join("?" for _ in data)
        upd = ", ".join(f"{c}=excluded.{c}" for c in data if c not in key)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT({', '.join(key)}) DO " + (f"UPDATE SET {upd}" if upd else "NOTHING")
        self.conn.execute(sql, list(data.values()))

    def update(self, table: str, where: dict, data: dict):
        sets = ", ".join(f"{k}=?" for k in data)
        cond = " AND ".join(f"{k}=?" for k in where)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {cond}", [_enc(v) for v in data.values()] + list(where.values()))

    # ---------------------------------------------------------------- kv / caches
    def kv_get(self, key, default=None):
        v = self.scalar("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(v) if v is not None else default

    def kv_set(self, key, value):
        self.upsert("kv", {"key": key, "value": json.dumps(value)}, ["key"])

    def load_http_cache(self) -> dict:
        return {r["url"]: {"etag": r["etag"], "last_modified": r["last_modified"]} for r in self.q("SELECT * FROM http_cache")}

    def save_http_cache(self, cache: dict):
        for url, v in cache.items():
            self.upsert("http_cache", {"url": url, "etag": v.get("etag"), "last_modified": v.get("last_modified"),
                                       "updated_at": now_iso()}, ["url"])

    def email_seen(self, message_id: str) -> bool:
        return self.scalar("SELECT 1 FROM email_seen WHERE message_id=?", (message_id,)) is not None

    def mark_email_seen(self, message_id: str):
        self.upsert("email_seen", {"message_id": message_id, "at": now_iso()}, ["message_id"])

    def llm_get(self, h):
        v = self.scalar("SELECT result FROM llm_cache WHERE content_hash=?", (h,))
        return json.loads(v) if v else None

    def llm_put(self, h, result):
        self.upsert("llm_cache", {"content_hash": h, "result": json.dumps(result), "at": now_iso()}, ["content_hash"])

    # ---------------------------------------------------------------- events
    def event(self, job_id: int, event: str, details: str = ""):
        self.conn.execute("INSERT INTO job_events (job_id, event, at, details) VALUES (?,?,?,?)", (job_id, event, now_iso(), details))

    def close(self):
        self.conn.commit()
        self.conn.close()

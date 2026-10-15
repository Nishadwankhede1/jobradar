"""Optional LLM semantic assessment. Off by default (provider = "none").

Providers:
  anthropic          – Claude Messages API (ANTHROPIC_API_KEY or LLM_API_KEY)
  openai_compatible  – any /chat/completions endpoint: Google Gemini free tier (default base_url), Groq, OpenRouter, OpenAI
                       (LLM_API_KEY / GEMINI_API_KEY / GROQ_API_KEY / OPENAI_API_KEY)
Results are cached per job content hash, so each vacancy version is assessed at most once.
The LLM only adjusts semantic sub-scores; deterministic hard eligibility rules are never overridden.
"""
from __future__ import annotations

import json
import os
import re

from ..http import HttpClient
from ..util import log, truncate

PROMPT = """You are a strict technical recruiter. Compare the CANDIDATE with the JOB and return ONLY JSON:
{{"subscores": {{"technical": 0-10, "responsibilities": 0-10, "experience": 0-10, "industry": 0-10, "career": 0-10}},
 "why": ["<=3 short reasons the candidate matches, citing CV evidence"],
 "gaps": ["<=3 short concrete gaps"],
 "eligibility_flags": ["only explicit restrictions stated in the JOB text, e.g. nationality, language, licence"],
 "confidence": "High|Medium|Low"}}
Rules: never invent facts not in the texts; do not guess salary or visa sponsorship; be conservative.

CANDIDATE (from CV):
{cv}

JOB: {title} at {company} ({location})
{description}
"""


def _cv_summary(profile: dict) -> str:
    c = profile["candidate"]
    skills = [k for k, v in profile["skills"].items() if v >= 0.7]
    degs = (profile.get("education") or {}).get("degrees") or []
    edu = "; ".join(f"{d.get('level', '')} {d.get('field', '')} ({d.get('status', '')} {d.get('year', '')})".strip() for d in degs)
    return (f"{c['headline']}, ~{c['years_experience']} years experience; industries: {', '.join(c['industries'])}; "
            f"languages: {', '.join(c['languages'])}; strong skills: {', '.join(skills)}.\n"
            f"Education: {edu}.\n"
            f"Experience highlights:\n{profile['experience_text']['text'].strip()[:1800]}")


def _key(provider: str) -> str | None:
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
    for k in ("LLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(k):
            return os.environ[k]
    return None


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d.get("subscores"), dict) else None
    except json.JSONDecodeError:
        return None


class LLMAssessor:
    def __init__(self, settings, http: HttpClient, db):
        self.cfg = settings.raw.get("llm", {})
        self.provider = os.environ.get("LLM_PROVIDER", self.cfg.get("provider", "none"))
        self.model = self.cfg.get("model")
        self.key = _key(self.provider)
        self.http, self.db, self.profile = http, db, settings.profile
        self.calls, self.max_calls = 0, int(self.cfg.get("max_calls_per_run", 25))
        self.enabled = self.provider != "none" and bool(self.key)

    def assess(self, job: dict, company_name: str, content_hash: str) -> dict | None:
        if not self.enabled:
            return None
        cached = self.db.llm_get(content_hash)
        if cached:
            return cached
        if self.calls >= self.max_calls:
            return None
        prompt = PROMPT.format(cv=_cv_summary(self.profile), title=job.get("title"), company=company_name,
                               location=job.get("location"), description=truncate(job.get("description") or "", 6000))
        self.calls += 1
        try:
            if self.provider == "anthropic":
                r = self.http.post_json("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": self.key, "anthropic-version": "2023-06-01"},
                    json_body={"model": self.model, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]})
                text = "".join(b.get("text", "") for b in r.get("content", []))
            else:
                base = self.cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
                r = self.http.post_json(f"{base}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                                        json_body={"model": self.model, "temperature": 0,
                                                   "messages": [{"role": "user", "content": prompt}]})
                text = r["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("LLM assessment failed (%s) – falling back to rules only", type(e).__name__)
            return None
        result = _parse(text)
        if result:
            self.db.llm_put(content_hash, result)
        return result

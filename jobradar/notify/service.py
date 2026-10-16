"""Decides WHAT to notify (thresholds, dedup, baselines, digests) and records every delivery attempt."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..db import Database
from ..util import iso, log, now_iso, parse_dt, redact, utcnow
from . import formatter
from .channels import Channel
from .formatter import DUBAI

ALERT_KINDS = ("alert", "baseline", "repost")


class Notifier:
    def __init__(self, settings, db: Database, channels: list[Channel]):
        self.s, self.db, self.channels = settings, db, channels

    # ------------------------------------------------------------------ helpers
    def _already(self, job_id: int, channel: str) -> bool:
        # the same vacancy may exist as several linked rows (official feed, employer e-mail, LinkedIn lead, agency
        # repost): one alert per vacancy, whichever row was alerted first
        group = list({job_id, *(r["id"] for r in self.db.q("SELECT id FROM jobs WHERE duplicate_of=?", (job_id,)))})
        ph = ",".join("?" * len(group))
        return self.db.scalar(f"SELECT 1 FROM notifications WHERE job_id IN ({ph}) AND channel=? AND (status='sent' OR "
                              f"(status='skipped' AND kind='repost')) AND kind IN ({','.join('?' * len(ALERT_KINDS))})",
                              (*group, channel, *ALERT_KINDS)) is not None

    def _attempts(self, job_id, channel) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(attempts),0) FROM notifications WHERE job_id=? AND channel=? AND status='failed'",
                                  (job_id, channel)) or 0)

    def _record(self, job_id, channel, kind, chash, status, ref="", error=None):
        prev = self.db.one("SELECT attempts FROM notifications WHERE job_id=? AND channel=? AND kind=? AND content_hash=?",
                           (job_id, channel, kind, chash))
        self.db.upsert("notifications", {"job_id": job_id, "channel": channel, "kind": kind, "content_hash": chash,
                                         "status": status, "attempts": (prev["attempts"] if prev else 0) + 1,
                                         "sent_at": now_iso() if status == "sent" else None, "error": redact(error),
                                         "message_ref": ref}, ["job_id", "channel", "kind", "content_hash"])
        self.db.commit()  # persist every delivery immediately – a killed run must not forget what was sent

    def candidates(self, min_score: float) -> list[dict]:
        rows = self.db.q(
            "SELECT j.*, s.score, s.label, s.reasons, s.gaps, s.uncertainties, s.recommendation, s.confidence, "
            "s.hard_disqualifiers, s.stage_a_pass FROM jobs j JOIN scores s ON s.job_id = j.id "
            "WHERE j.status='active' AND j.duplicate_of IS NULL AND s.stage_a_pass=1 AND s.score >= ? "
            "ORDER BY s.score DESC", (min_score,))
        return [r for r in rows if not r["hard_disqualifiers"]]

    def _attach_cv(self, ch, j):
        """Send the tailored CV right under the alert (Telegram only; never blocks the alert itself)."""
        if not hasattr(ch, "send_document"):
            return
        try:
            from ..config import ROOT
            r = self.db.one("SELECT file, coverage_before, coverage_after FROM resumes WHERE job_id=?", (j["id"],))
            if not r or not r["file"]:
                return
            f = ROOT / self.s.get("resume", "output_dir", "output/resumes") / r["file"]
            if f.is_file():
                ch.send_document(f, f"Tailored CV for this role · ATS keywords {r['coverage_before']}% → {r['coverage_after']}%")
        except Exception as e:
            log.warning("CV attachment failed: %s", type(e).__name__)

    # ------------------------------------------------------------------ immediate alerts
    def process_alerts(self) -> tuple[int, int]:
        if not self.channels:
            return 0, 0
        thr = float(self.s.get("scoring", "immediate_threshold", 7.0))
        cap = int(self.s.get("notify", "max_alerts_per_run", 15))
        on_baseline = bool(self.s.get("notify", "alert_on_baseline", False))
        sent = failed = 0
        from ..applications import all_applications, match
        apps = all_applications(self.db)
        # job-alert e-mail / aggregator leads: instant alert too (being early matters), but only fresh ones, never old backlog
        leads_ok = bool(self.s.get("notify", "instant_alerts_for_leads", True))
        lead_max_age = timedelta(hours=float(self.s.get("notify", "lead_alert_max_age_hours", 48)))
        def lead_fresh(j):
            t = parse_dt(j.get("first_seen_at"))
            return leads_ok and t is not None and utcnow() - t <= lead_max_age
        for ch in self.channels:
            baseline_rows, n, channel_down = [], 0, False
            for j in self.candidates(thr):
                if (not j["is_official"] and not lead_fresh(j)) or self._already(j["id"], ch.name) or self._attempts(j["id"], ch.name) >= 5:
                    continue
                if apps and match(j, apps):  # you already applied to this job (or the same role at this employer)
                    continue
                if j["baseline"] and not on_baseline:
                    baseline_rows.append(j)
                    continue
                kind = "alert"
                if j["reposted_from"]:
                    prev = self.db.one("SELECT sent_at FROM notifications WHERE job_id=? AND channel=? AND status='sent'",
                                       (j["reposted_from"], ch.name))
                    if prev and parse_dt(prev["sent_at"]) and utcnow() - parse_dt(prev["sent_at"]) < timedelta(days=30):
                        self._record(j["id"], ch.name, "repost", j["content_hash"], "skipped", error="repost of recently notified job")
                        continue
                    kind = "repost" if prev else "alert"
                if n >= cap:
                    log.info("alert cap (%d) reached – remaining alerts deferred to next run", cap)
                    break
                html_msg, plain = formatter.job_alert(j, j, kind)
                try:
                    ref = ch.send(html_msg, plain)
                    self._record(j["id"], ch.name, kind, j["content_hash"], "sent", ref)
                    self._attach_cv(ch, j)
                    sent += 1
                    n += 1
                except Exception as e:
                    self._record(j["id"], ch.name, kind, j["content_hash"], "failed", error=f"{type(e).__name__}: {str(e)[:200]}")
                    failed += 1
                    log.warning("%s delivery failed: %s – pausing this channel until the next run", ch.name, type(e).__name__)
                    channel_down = True
                    break
            if baseline_rows and not channel_down:
                top = int(self.s.get("notify", "baseline_summary_top", 10))
                h = [f"<b>JobRadar is live – initial scan</b>",
                     f"{len(baseline_rows)} existing vacancies scored ≥ {thr:g}. Top {min(top, len(baseline_rows))}:"]
                p = [h[0].replace("<b>", "").replace("</b>", ""), h[1]]
                for r in baseline_rows[:top]:
                    h.append(f"• <b>{r['score']:.1f}</b> {formatter._e(r['title'])} – {formatter._e(r['company_display'])}\n  {formatter._e(r['url'])}")
                    p.append(f"- {r['score']:.1f} {r['title']} – {r['company_display']}\n  {r['url']}")
                h.append("\nFrom now on you'll get one alert per NEW matching vacancy.")
                p.append("\nFrom now on you'll get one alert per NEW matching vacancy.")
                try:
                    ref = ch.send("\n".join(h), "\n".join(p))
                    for r in baseline_rows:
                        self._record(r["id"], ch.name, "baseline", r["content_hash"], "sent", ref)
                    sent += 1
                except Exception as e:
                    failed += 1
                    log.warning("%s baseline summary failed: %s", ch.name, type(e).__name__)
        self.db.commit()
        return sent, failed

    # ------------------------------------------------------------------ digest
    def health(self) -> dict:
        d = self.db
        return {"monitored": d.scalar("SELECT COUNT(*) FROM sources s JOIN companies c ON c.id=s.company_id WHERE c.enabled=1 AND c.ats NOT IN ('manual','email')") or 0,
                "ok": d.scalar("SELECT COUNT(*) FROM sources WHERE last_status IN ('ok','not_modified','partial')") or 0,
                "failing": d.scalar("SELECT COUNT(*) FROM sources WHERE last_status='error'") or 0,
                "jobs_total": d.scalar("SELECT COUNT(*) FROM jobs") or 0,
                "last_scan": formatter._dubai(d.scalar("SELECT MAX(finished_at) FROM scan_runs")) or "never"}

    def maybe_send_digest(self, force=False) -> int:
        if not self.channels:
            return 0
        local = datetime.now(DUBAI)
        if not force and local.hour < int(self.s.get("notify", "digest_hour_dubai", 9)):
            return 0
        date = local.strftime("%Y-%m-%d")
        since = iso(utcnow() - timedelta(hours=24))
        min_s = float(self.s.get("scoring", "digest_min_score", 6.0))
        from ..applications import all_applications, match
        apps = all_applications(self.db)
        rows = [r for r in self.candidates(min_s) if (r["first_seen_at"] or "") >= since and not (apps and match(r, apps))]
        official = [r for r in rows if r["is_official"]]
        leads = [r for r in rows if not r["is_official"]] if self.s.get("notify", "include_unverified_leads_in_digest", True) else []
        n = 0
        for ch in self.channels:
            if not force and self.db.scalar("SELECT 1 FROM digests WHERE digest_date=? AND channel=? AND status='sent'", (date, ch.name)):
                continue
            h, p = formatter.digest(local.strftime("%a %d %b %Y"), official, leads, self.health())
            try:
                ch.send(h, p)
                status, err = "sent", None
                n += 1
            except Exception as e:
                status, err = "failed", type(e).__name__
            self.db.upsert("digests", {"digest_date": date, "channel": ch.name, "status": status, "sent_at": now_iso(),
                                       "job_count": len(official) + len(leads), "error": err}, ["digest_date", "channel"])
        self.db.commit()
        return n

    def send_test(self) -> list[str]:
        sample = {"company_display": "JobRadar test", "title": "Business Intelligence Analyst (TEST MESSAGE)", "city": "Dubai",
                  "employment_type": "Full-time", "posted_at": now_iso(), "first_seen_at": now_iso(), "source": "greenhouse",
                  "url": "https://example.com/this-is-a-test", "is_agency": 0}
        sc = {"score": 8.7, "label": "Strong match", "confidence": "High",
              "reasons": ["This is a delivery test – no real vacancy", "Power BI, SQL, Snowflake overlap"],
              "gaps": ["None – test"], "uncertainties": [], "recommendation": "Ignore – test message"}
        out = []
        for ch in self.channels:
            h, p = formatter.job_alert(sample, sc)
            try:
                out.append(f"{ch.name}: sent (id {ch.send(h, p)})")
            except Exception as e:
                out.append(f"{ch.name}: FAILED {type(e).__name__}: {str(e)[:150]}")
        return out

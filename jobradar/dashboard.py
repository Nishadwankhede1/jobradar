"""Static monitoring dashboard (site/index.html + site/data.json). Works on GitHub Pages, locally, or as an Artifact."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from .db import Database
from datetime import timedelta

from .util import iso, now_iso, utcnow

OK_STATES = ("ok", "not_modified", "partial")


def coverage_stats(db: Database, notify_threshold: float = 7.0) -> dict:
    s = db.scalar
    lat = [r["latency_minutes"] for r in db.q("SELECT latency_minutes FROM jobs WHERE latency_minutes IS NOT NULL")]
    relevant = db.q("SELECT s.hard_disqualifiers FROM jobs j JOIN scores s ON s.job_id=j.id WHERE j.status='active' "
                    "AND j.duplicate_of IS NULL AND s.stage_a_pass=1 AND s.score >= ?", (notify_threshold,))
    return {
        "generated_at": now_iso(),
        "companies_discovered": s("SELECT COUNT(*) FROM companies WHERE kind='employer' AND enabled=1"),
        "uae_jobs_last_check": s("SELECT COALESCE(SUM(s.last_job_count),0) FROM sources s JOIN companies c ON c.id=s.company_id "
                                 "WHERE c.enabled=1 AND s.last_status IN ('ok','not_modified','partial')"),
        "jobs_new_24h": s("SELECT COUNT(*) FROM jobs WHERE status='active' AND duplicate_of IS NULL AND first_seen_at >= ?",
                          (iso(utcnow() - timedelta(hours=24)),)),
        "companies_from_leads": s("SELECT COUNT(*) FROM companies WHERE kind = 'lead'"),
        "companies_with_feed": s("SELECT COUNT(*) FROM companies WHERE kind='employer' AND ats NOT IN ('manual','email') AND enabled=1"),
        "sources_monitored_ok": s(f"SELECT COUNT(*) FROM sources s JOIN companies c ON c.id=s.company_id WHERE c.enabled=1 AND s.last_status IN {OK_STATES}"),
        "sources_failing": s("SELECT COUNT(*) FROM sources s JOIN companies c ON c.id=s.company_id WHERE c.enabled=1 AND s.last_status='error'"),
        "sources_unmonitorable": s("SELECT COUNT(*) FROM sources s JOIN companies c ON c.id=s.company_id WHERE c.enabled=1 AND c.kind='employer' AND s.last_status='unmonitored'"),
        "sources_pending_first_scan": s("SELECT COUNT(*) FROM sources s JOIN companies c ON c.id=s.company_id "
                                        "WHERE s.last_status IS NULL AND c.ats NOT IN ('manual','email')"),
        "vacancies_analyzed": s("SELECT COUNT(*) FROM jobs"),
        "vacancies_active": s("SELECT COUNT(*) FROM jobs WHERE status='active' AND duplicate_of IS NULL"),
        "vacancies_closed": s("SELECT COUNT(*) FROM jobs WHERE status='closed'"),
        "duplicates_linked": s("SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL"),
        "relevant_jobs": sum(1 for r in relevant if not r["hard_disqualifiers"]),
        "ineligible_hard_restriction": s("SELECT COUNT(*) FROM scores WHERE hard_disqualifiers NOT IN ('[]','') AND hard_disqualifiers IS NOT NULL"),
        "alerts_sent": s("SELECT COUNT(*) FROM notifications WHERE status='sent'"),
        "alerts_failed": s("SELECT COUNT(*) FROM notifications WHERE status='failed'"),
        "digests_sent": s("SELECT COUNT(*) FROM digests WHERE status='sent'"),
        "scan_runs": s("SELECT COUNT(*) FROM scan_runs"),
        "last_successful_scan": s("SELECT MAX(last_success_at) FROM sources"),
        "median_detection_latency_min": round(statistics.median(lat), 1) if lat else None,
        "latency_samples": len(lat),
    }


def dashboard_data(db: Database, settings) -> dict:
    thr = float(settings.get("scoring", "notify_threshold", 7.0))
    cols = """j.id, j.company_display, j.title, j.city, j.location, j.url, j.source, j.verification, j.is_official,
        j.is_agency, j.first_seen_at, j.posted_at, j.status, j.employment_type, j.reposted_from, j.evidence,
        s.score, s.label, s.stage_a_pass, s.stage_a_reason, s.hard_disqualifiers, s.subscores, s.reasons, s.gaps,
        s.uncertainties, s.matched_skills, s.missing_skills, s.recommendation, s.confidence, s.method,
        (SELECT MAX(sent_at) FROM notifications n WHERE n.job_id=j.id AND n.status='sent') AS notified_at,
        c.name AS company_name, j.company_id, j.norm_title, j.fingerprint, r.file AS cv_file, r.coverage_before AS cv_before, r.coverage_after AS cv_after,
        r.added AS cv_added, r.missing AS cv_missing"""
    base = ("FROM jobs j JOIN scores s ON s.job_id=j.id LEFT JOIN companies c ON c.id=j.company_id "
            "LEFT JOIN resumes r ON r.job_id=j.id WHERE j.status='active' AND j.duplicate_of IS NULL")
    # best matches + the most recently posted jobs (so "Latest posts" isn't limited to high scorers)
    jobs = db.q(f"SELECT {cols} {base} ORDER BY s.score DESC LIMIT 600")
    seen = {j["id"] for j in jobs}
    jobs += [j for j in db.q(f"SELECT {cols} {base} ORDER BY COALESCE(NULLIF(j.posted_at,''), j.first_seen_at) DESC LIMIT 600")
             if j["id"] not in seen]
    sources = db.q("""SELECT c.id, c.name, c.industry, c.ats, c.priority, c.kind, c.careers_url, c.verified, s.last_status,
        s.last_success_at, s.last_run_at, s.last_error, s.consecutive_failures, s.last_job_count, s.interval_minutes,
        s.next_due_at, s.last_duration_ms,
        (SELECT COUNT(*) FROM jobs j WHERE j.company_id=c.id AND j.status='active' AND j.duplicate_of IS NULL) AS jobs_active,
        (SELECT MAX(sc.score) FROM jobs j JOIN scores sc ON sc.job_id=j.id WHERE j.company_id=c.id AND j.status='active'
           AND j.duplicate_of IS NULL AND sc.hard_disqualifiers IN ('[]','')) AS best_score
        FROM companies c LEFT JOIN sources s ON s.company_id=c.id
        WHERE c.kind = 'employer' AND c.enabled=1
        ORDER BY CASE s.last_status WHEN 'error' THEN 0 WHEN 'partial' THEN 1 ELSE 2 END, c.priority, c.name""")
    runs = db.q("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 30")
    from .applications import all_applications
    apps = all_applications(db)
    return {"stats": coverage_stats(db, thr), "jobs": jobs, "sources": sources, "runs": runs, "applications": apps,
            "threshold": thr, "title": settings.get("dashboard", "title", "JobRadar")}


STYLE = r"""
<title>JobRadar Dubai</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,500..800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: monitoring console – status strip, then ranked matches (expandable rows), then source health and scan log */
:root{
  --bg:#f3f5f4; --surface:#ffffff; --sunk:#e8eceb; --line:#d3dad8; --fg:#14201f; --muted:#5a6967;
  --accent:#0d6e6a; --accent-ink:#ffffff; --accent-soft:#d4ebe8;
  --good:#1f7a3f; --good-soft:#dcefe2; --warn:#9a6200; --warn-soft:#f6ead0; --bad:#b3261e; --bad-soft:#f7dedb;
  --display:"Archivo","Arial Narrow",system-ui,sans-serif; --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0e1514; --surface:#151f1e; --sunk:#1c2928; --line:#2b3a38; --fg:#e3ecea; --muted:#93a5a2;
  --accent:#4fc1b8; --accent-ink:#06201e; --accent-soft:#173a37;
  --good:#6fcf8e; --good-soft:#173524; --warn:#e3b04f; --warn-soft:#3a2d12; --bad:#f08a80; --bad-soft:#3d1a17; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0e1514; --surface:#151f1e; --sunk:#1c2928; --line:#2b3a38; --fg:#e3ecea; --muted:#93a5a2;
  --accent:#4fc1b8; --accent-ink:#06201e; --accent-soft:#173a37;
  --good:#6fcf8e; --good-soft:#173524; --warn:#e3b04f; --warn-soft:#3a2d12; --bad:#f08a80; --bad-soft:#3d1a17; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.5 var(--body);margin:0}
.wrap{max-width:1180px;margin:0 auto;padding-inline:20px;padding-block:28px 64px;display:grid;gap:28px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px}
h1{font:800 clamp(28px,4vw,40px)/1 var(--display);font-stretch:80%;letter-spacing:-.01em;margin:0;text-wrap:balance}
h1 span{color:var(--accent)}
h2{font:700 20px/1.2 var(--display);font-stretch:85%;margin:0 0 10px;text-wrap:balance}
.sub{color:var(--muted);font-size:13px;margin-top:6px}
.mono,.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));border:1px solid var(--line);background:var(--surface);border-radius:6px}
.strip div{padding:12px 14px;border-right:1px solid var(--line);min-width:0}
.strip div:last-child{border-right:0}
.strip b{display:block;font:600 22px/1.1 var(--mono);font-variant-numeric:tabular-nums}
.strip small{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.strip .bad b{color:var(--bad)}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin-bottom:10px;font-size:13px}
.controls input[type=search]{flex:1 1 220px;min-width:0;padding:8px 10px;border:1px solid var(--line);border-radius:4px;background:var(--surface);color:var(--fg);font:inherit}
.controls label{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:var(--muted);min-width:0}
.controls input[type=range]{max-width:140px}
input:focus-visible,button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{font:600 11px/1 var(--body);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);text-align:left;padding:10px 12px;background:var(--sunk);white-space:nowrap}
td{padding:10px 12px;border-top:1px solid var(--line);vertical-align:top}
tr.job{cursor:pointer}
tr.job:hover td{background:var(--sunk)}
.pill{display:inline-block;min-width:44px;text-align:center;padding:3px 8px;border-radius:99px;font:600 13px/1.2 var(--mono);font-variant-numeric:tabular-nums}
.s9{background:var(--accent);color:var(--accent-ink)} .s8{background:var(--accent-soft);color:var(--accent)}
.s7{background:var(--good-soft);color:var(--good)} .s6{background:var(--warn-soft);color:var(--warn)} .s0{background:var(--sunk);color:var(--muted)}
.chip{display:inline-block;font-size:11px;padding:2px 7px;border-radius:3px;margin:2px 4px 0 0;background:var(--sunk);color:var(--muted);white-space:nowrap}
.chip.bad{background:var(--bad-soft);color:var(--bad)} .chip.good{background:var(--good-soft);color:var(--good)} .chip.warn{background:var(--warn-soft);color:var(--warn)}
.t{font-weight:600} .co{color:var(--muted);font-size:13px}
a{color:var(--accent);overflow-wrap:anywhere}
section{min-width:0}
.detail td{background:var(--sunk);padding:14px 16px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.dgrid h3{font:600 12px/1 var(--body);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
.dgrid ul{margin:0;padding-left:18px}
.bar{display:grid;grid-template-columns:120px 1fr 34px;gap:8px;align-items:center;font-size:12px;margin-bottom:4px}
.bar i{display:block;height:6px;border-radius:3px;background:var(--line);position:relative;overflow:hidden}
.bar i em{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:3px}
.st{font-size:12px;font-weight:600}
.st.ok{color:var(--good)} .st.error{color:var(--bad)} .st.partial,.st.not_modified{color:var(--warn)} .st.none{color:var(--muted)}
details>summary{cursor:pointer;font:700 20px/1.2 var(--display);font-stretch:85%;margin-bottom:10px}
.empty{padding:24px;color:var(--muted)}
.foot{color:var(--muted);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px;display:grid;gap:4px;min-width:0}
.kpi small{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em;font-weight:600}
.kpi b{font:600 30px/1.05 var(--mono);font-variant-numeric:tabular-nums;color:var(--fg)}
.kpi span{color:var(--muted);font-size:12px}
.kpi.accent{border-color:var(--accent);box-shadow:inset 0 3px 0 var(--accent)} .kpi.accent b{color:var(--accent)}
.kpi.good span{color:var(--good)} .kpi.bad span,.kpi.bad b{color:var(--bad)}
.health{margin-top:14px;display:grid;gap:8px}
.hbar{display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;background:var(--surface)}
.hbar i{display:block;min-width:4px}
.hbar .ok,.hleg .ok{background:var(--good)} .hbar .bad,.hleg .bad{background:var(--bad)} .hbar .al,.hleg .al{background:var(--line)}
.hleg{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px;color:var(--fg)}
.hleg span{display:inline-flex;align-items:center;gap:6px} .hleg i{width:10px;height:10px;border-radius:2px;display:inline-block}
.hleg .muted{color:var(--muted)}
.tabs{display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--line)}
.tabs button{font:600 14px/1 var(--body);background:none;border:0;border-bottom:3px solid transparent;color:var(--muted);padding:12px 14px;cursor:pointer;margin-bottom:-1px}
.tabs button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent)}
.tabs .n,.seg .n{font:500 12px var(--mono);color:var(--muted);margin-left:4px}
.seg{display:flex;flex-wrap:wrap;gap:6px}
.seg button{font:500 13px var(--body);border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:99px;padding:6px 12px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)} .seg button[aria-pressed=true] .n{color:inherit}
.shown{color:var(--muted);font-size:12px;margin-left:auto}
th.num,td.num{text-align:right}
.st.good{color:var(--good)} .st.bad{color:var(--bad)} .st.warn{color:var(--warn)} .st.muted{color:var(--muted)}
.tick{width:30px;height:30px;border-radius:6px;border:1.5px solid var(--line);background:var(--surface);color:transparent;
  font:700 16px/1 var(--body);cursor:pointer;display:inline-grid;place-items:center}
.tick:hover{border-color:var(--accent)} .tick[aria-pressed=true]{background:var(--good);border-color:var(--good);color:#fff}
.tick:disabled{opacity:.45;cursor:not-allowed}
tr.job.done .t{color:var(--muted)}
.note{font-size:13px;color:var(--muted);margin:0 0 10px}
.note.warn{color:var(--warn)}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent);margin-top:8px}
#cos td:first-child{min-width:250px}
.plinks a{color:var(--accent);text-decoration:none;white-space:nowrap}.plinks a:hover{text-decoration:underline}
.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);padding:8px 12px;border-radius:5px;text-decoration:none;font-weight:600}
[hidden]{display:none!important}
.age{font-size:12px;color:var(--muted);margin-top:2px}.age.fresh{color:var(--good);font-weight:600}.age.stale{color:var(--warn)}
.only-sm{display:none}
select{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:4px;background:var(--surface);color:var(--fg)}
@media (max-width:640px){.hide-sm{display:none}.only-sm{display:block}.kpi b{font-size:24px}.bar{grid-template-columns:100px 1fr 30px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
"""

BODY = r"""
<div class="wrap">
  <header>
    <div><h1>Job<span>Radar</span> · Dubai</h1>
      <div class="sub" id="subline"></div></div>
    <div class="sub mono" id="gen"></div>
  </header>
  <section aria-label="Overview">
    <div class="kpis" id="kpis"></div>
    <div class="health" id="health"></div>
  </section>
  <nav class="tabs" role="tablist" aria-label="Views">
    <button role="tab" data-tab="rel" aria-selected="true">Relevant jobs <span class="n" id="n-rel"></span></button>
    <button role="tab" data-tab="new" aria-selected="false">Latest posts <span class="n" id="n-new"></span></button>
    <button role="tab" data-tab="all" aria-selected="false">All UAE jobs <span class="n" id="n-all"></span></button>
    <button role="tab" data-tab="app" aria-selected="false">Applied <span class="n" id="n-app"></span></button>
    <button role="tab" data-tab="co" aria-selected="false">Companies <span class="n" id="n-co"></span></button>
    <button role="tab" data-tab="log" aria-selected="false">Scan log</button>
  </nav>
  <section id="tab-jobs">
    <div class="controls">
      <input type="search" id="q" placeholder="Search title, company, skill…" aria-label="Search jobs">
      <label for="min">Min score <input type="range" id="min" min="0" max="10" step="0.5" value="7"><span class="mono" id="minv">7.0</span></label>
      <label for="inel"><input type="checkbox" id="inel"> show ineligible</label>
      <label for="sort">Sort <select id="sort"><option value="new">Newest posted first</option><option value="score">Best match first</option></select></label>
      <label for="age">Posted <select id="age"><option value="0">any time</option><option value="7">last 7 days</option><option value="30">last 30 days</option><option value="90">last 90 days</option></select></label>
      <label for="leads"><input type="checkbox" id="leads" checked> include LinkedIn / e-mail leads</label>
      <label for="hideapp"><input type="checkbox" id="hideapp" checked> hide jobs I applied to</label>
      <span class="shown" id="jshown"></span>
    </div>
    <div class="scroll"><table><thead><tr><th>Score</th><th>Role</th><th class="hide-sm">Where</th><th class="hide-sm">Posted</th><th>Advice</th><th title="Tick when you have applied">Applied</th></tr></thead>
      <tbody id="jobs"></tbody></table></div>
    <p class="foot">Click a row for the full match explanation and the apply link. 9–10 exceptional · 8–8.9 strong · 7–7.9 good · 6–6.9 possible. Telegram alerts fire at ≥ <span id="thr"></span>.</p>
  </section>
  <section id="tab-co" hidden>
    <div class="controls">
      <input type="search" id="cq" placeholder="Search company, industry, job board…" aria-label="Search companies">
      <div class="seg" role="group" aria-label="Status filter" id="cseg"></div>
      <span class="shown" id="cshown"></span>
    </div>
    <div class="scroll"><table><thead><tr><th>Company</th><th>How it's tracked</th><th>Status</th><th class="num">UAE jobs</th><th class="num hide-sm">Scored</th><th class="num hide-sm">Best</th><th class="hide-sm">Last check</th></tr></thead>
      <tbody id="cos"></tbody></table></div>
    <p class="foot">"UAE jobs" is the count at the employer's last check. "Scored" are the vacancies JobRadar has read and scored for you. Companies on "LinkedIn / e-mail alerts" have no readable job board; their jobs arrive through alert e-mails.</p>
  </section>
  <section id="tab-app" hidden>
    <p class="note" id="appmode"></p>
    <div class="scroll"><table><thead><tr><th>Applied on</th><th>Role</th><th class="num">Score</th><th>Stage</th><th class="hide-sm">Posting</th><th class="hide-sm">Your CV</th><th>Applied</th></tr></thead>
      <tbody id="apps"></tbody></table></div>
    <p class="foot">Untick to move a job back to the job lists. A new posting for the same role at the same employer (a repost, or the LinkedIn copy of a company-site job) is flagged "Already applied" and never sends another alert.</p>
  </section>
  <section id="tab-log" hidden>
    <div class="scroll"><table><thead><tr><th>Finished</th><th class="num">Sources</th><th class="num">Failed</th><th class="num">New</th><th class="num">Closed</th><th class="num">Alerts</th><th class="num hide-sm">Requests</th></tr></thead>
      <tbody id="runs"></tbody></table></div>
  </section>
  <p class="foot">Hard restrictions (nationality, mandatory Arabic, location) always override the score. Posting dates come from the employer's own posting; when an employer doesn't publish one, the date JobRadar first saw the job is used (after the first scan). Leads from LinkedIn or alert e-mails are marked until the employer's own posting is found.</p>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => n == null ? '—' : Number(n).toLocaleString('en-US');
const dubai = ts => { if(!ts) return '—'; const d = new Date(ts); if (isNaN(d)) return esc(ts);
  return d.toLocaleString('en-GB',{timeZone:'Asia/Dubai',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}); };
const ago = ts => { if(!ts) return 'never'; const m = (Date.now()-new Date(ts))/60000; if (m < 0) return 'just now';
  return m < 90 ? Math.round(m)+' min ago' : m < 2880 ? Math.round(m/60)+' h ago' : Math.round(m/1440)+' d ago'; };
const cls = s => s >= 9 ? 's9' : s >= 8 ? 's8' : s >= 7 ? 's7' : s >= 6 ? 's6' : 's0';
const S = D.stats, thr = D.threshold;
const eligible = j => !(j.hard_disqualifiers||[]).length && j.stage_a_pass;
const relevant = D.jobs.filter(j => eligible(j) && j.score >= thr);
/* posting age: the employer's own posted date; without one, JobRadar's first sighting counts only after the first
   (baseline) scan – on day one every job is "first seen" today, which says nothing about when it was posted */
const DAY = 86400000;
const firstScan = D.jobs.reduce((m, j) => j.first_seen_at && j.first_seen_at < m ? j.first_seen_at : m, '9999');
const baseline = new Date(firstScan).getTime() + DAY;
function postedTs(j){
  if (j.posted_at) { const t = new Date(j.posted_at).getTime(); if (!isNaN(t)) return t; }
  const f = new Date(j.first_seen_at).getTime();
  return !isNaN(f) && f > baseline ? f : null;
}
const GST = 4 * 3600000;  // calendar days in Dubai time
function relAge(t){ const d = Math.floor((Date.now() + GST) / DAY) - Math.floor((t + GST) / DAY);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 14 ? d + ' days ago' : d < 60 ? Math.round(d/7) + ' weeks ago'
    : d < 365 ? Math.round(d/30) + ' months ago' : 'over a year ago'; }
function ageHtml(j, full){
  const t = postedTs(j);
  if (t == null) return `<div class="age">date not published</div>${full ? '<div class="co mono">seen ' + dubai(j.first_seen_at) + '</div>' : ''}`;
  const d = (Date.now() - t) / DAY, c = d <= 7 ? 'fresh' : d > 45 ? 'stale' : '';
  const src = j.posted_at ? 'Posted ' : 'First seen ';
  return `<div class="age ${c}">${src}${relAge(t)}</div>${full ? '<div class="co mono">' + new Date(t).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric',timeZone:'Asia/Dubai'}) + '</div>' : ''}`;
}
$('subline').textContent = 'Tuned to a BI / Power BI / SQL / data analyst profile · scores every UAE vacancy from the companies below';
$('gen').textContent = 'Updated ' + dubai(S.generated_at) + ' GST · last feed check ' + ago(S.last_successful_scan);
$('thr').textContent = thr.toFixed(1);
const live = S.sources_monitored_ok, fail = S.sources_failing, feeds = live + fail;
const fresh7 = relevant.filter(j => { const t = postedTs(j); return t != null && Date.now() - t <= 7 * DAY; }).length;
const K = [
  ['Companies tracked', fmt(S.companies_discovered), 'employers in Dubai & the UAE'],
  ['Live job feeds', fmt(live), fail ? fmt(fail) + ' of ' + fmt(feeds) + ' failing' : 'all ' + fmt(feeds) + ' answering', fail ? 'bad' : 'good'],
  ['UAE jobs found', fmt(S.uae_jobs_last_check), 'open roles at last check'],
  ['Jobs scored', fmt(S.vacancies_active), fmt(fresh7) + ' relevant posted in last 7 days'],
  ['Relevant for you', fmt(relevant.length), 'score ≥ ' + thr + ', eligible', 'accent'],
  ['Alerts sent', fmt(S.alerts_sent), S.alerts_failed ? fmt(S.alerts_failed) + ' failed' : 'Telegram'],
];
$('kpis').innerHTML = K.map(([k,v,sub,c]) => `<div class="kpi ${c||''}"><small>${esc(k)}</small><b>${esc(v)}</b><span>${esc(sub)}</span></div>`).join('');
const alertsOnly = S.sources_unmonitorable;
const tot = live + fail + alertsOnly || 1;
$('health').innerHTML = `<div class="hbar" role="img" aria-label="${live} live feeds, ${fail} failing, ${alertsOnly} on alerts">
    <i class="ok" style="flex:${live}"></i>${fail ? `<i class="bad" style="flex:${fail}"></i>` : ''}<i class="al" style="flex:${alertsOnly}"></i></div>
  <div class="hleg"><span><i class="ok"></i>Live feed OK · ${fmt(live)}</span>${fail ? `<span><i class="bad"></i>Failing · ${fmt(fail)}</span>` : ''}<span><i class="al"></i>LinkedIn / e-mail alerts · ${fmt(alertsOnly)}</span>
  <span class="muted">${Math.round(100*live/tot)}% of companies read automatically</span></div>`;

/* ---------------- jobs */
function rowHtml(j){
  const hard = (j.hard_disqualifiers||[]); const chips = [];
  if (hard.length) chips.push('<span class="chip bad">✕ Ineligible</span>');
  if (!j.is_official) chips.push('<span class="chip warn">Lead · ' + esc(String(j.source||'').replace('email:','') ) + '</span>');
  if (j.is_agency) chips.push('<span class="chip warn">Agency</span>');
  if (j.reposted_from) chips.push('<span class="chip">Reposted</span>');
  if (j.notified_at) chips.push('<span class="chip good">✓ Alerted</span>');
  const ap = appFor(j);
  if (ap && ap.self) chips.push('<span class="chip good">✓ Applied ' + esc(dubai(ap.a.applied_at).slice(0,6)) + '</span>');
  else if (ap) chips.push('<span class="chip warn" title="' + esc(ap.a.title + ' · ' + ap.a.company) + '">⚠ Already applied – same role (' + esc(dubai(ap.a.applied_at).slice(0,6)) + ')</span>');
  return `<tr class="job${ap ? ' done' : ''}" data-id="${j.id}" tabindex="0" aria-expanded="${open.has(j.id)}"><td><span class="pill ${hard.length?'s0':cls(j.score)}">${(+j.score).toFixed(1)}</span></td>
   <td><div class="t">${esc(j.title)}</div><div class="co">${esc(j.company_display)}</div><div class="only-sm">${ageHtml(j)}</div>${chips.join('')}</td>
   <td class="hide-sm co">${esc(j.city || j.location || '')}</td>
   <td class="hide-sm">${ageHtml(j, true)}</td>
   <td class="co">${esc(j.recommendation)}</td><td>${tickHtml(j)}</td></tr>`;
}
function detailHtml(j){
  const sub = j.subscores||{};
  const bars = ['technical','responsibilities','experience','eligibility','industry','career'].map(k =>
    `<div class="bar"><span>${k}</span><i><em style="width:${(sub[k]||0)*10}%"></em></i><span class="mono">${(sub[k]??0).toFixed(1)}</span></div>`).join('');
  const li = a => (a&&a.length) ? '<ul>'+a.map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>' : '<div class="co">None</div>';
  return `<tr class="detail"><td colspan="6"><div class="dgrid">
    <div><h3>Score breakdown · ${esc(j.label)} · ${esc(j.confidence)} confidence</h3>${bars}</div>
    <div><h3>Why you match</h3>${li(j.reasons)}<h3 style="margin-top:12px">Skills you have</h3><div>${(j.matched_skills||[]).map(s=>`<span class="chip good">${esc(s)}</span>`).join('')||'<span class="co">—</span>'}</div></div>
    <div><h3>Gaps</h3>${li(j.gaps)}<h3 style="margin-top:12px">Check before applying</h3>${li([...(j.hard_disqualifiers||[]), ...(j.uncertainties||[])])}</div>
    <div><h3>Apply</h3><a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Open the job posting ↗</a><div class="co" style="margin-top:8px">${esc(j.evidence||'')}</div>
      ${cvHtml(j)}
      <h3 style="margin-top:14px">Who to contact (LinkedIn, UAE)</h3><div class="co">${peopleHtml(j)}</div></div>
  </div></td></tr>`;
}
/* LinkedIn people searches (UAE) – opens LinkedIn's own search; nothing about people is collected here */
const UAE = '%5B%22104305776%22%5D';
function coName(j){ return String(j.company_name || j.company_display || j.name || '').replace(/\(.*?\)/g, ' ').split(/ \/ | \| | - /)[0]
  .replace(/\b(LLC|L\.L\.C|PJSC|FZ-?LLC|FZE|DMCC|Ltd|Limited|Inc)\b\.?/gi, '').replace(/\s+/g, ' ').trim(); }
function liPeople(co, kind){
  const kw = kind === 'rec' ? `"${co}" (recruiter OR "talent acquisition" OR "talent partner")`
    : `"${co}" ("head of data" OR "head of analytics" OR "analytics manager" OR "BI manager" OR "data manager" OR "business intelligence manager" OR "reporting manager")`;
  return 'https://www.linkedin.com/search/results/people/?keywords=' + encodeURIComponent(kw) + '&geoUrn=' + UAE + '&origin=FACETED_SEARCH';
}
function gPeople(co){ return 'https://www.google.com/search?q=' + encodeURIComponent(`site:linkedin.com/in "${co}" ("head of data" OR "analytics manager" OR "BI manager") (Dubai OR UAE)`); }
function peopleHtml(o){ const co = coName(o); if (!co) return '';
  return `<span class="plinks"><a href="${liPeople(co,'hm')}" target="_blank" rel="noopener">Hiring managers ↗</a> · <a href="${liPeople(co,'rec')}" target="_blank" rel="noopener">Recruiters ↗</a> · <a href="${gPeople(co)}" target="_blank" rel="noopener">Google ↗</a></span>`; }
function cvHtml(j){
  if (j.cv_after == null) return `<h3 style="margin-top:14px">Tailored CV</h3><div class="co">${j.score >= thr && eligible(j) ? 'Created on your next scan (run_jobradar.bat).' : 'Made automatically for jobs scoring ≥ ' + thr + '.'}</div>`;
  const chips = (a, c) => (a||[]).map(x => `<span class="chip ${c}">${esc(x)}</span>`).join('');
  return `<h3 style="margin-top:14px">Tailored CV · ATS keywords ${j.cv_before}% → <b>${j.cv_after}%</b></h3>
    ${cvHref(j.cv_file) ? `<a class="btn ghost" href="${esc(cvHref(j.cv_file))}" data-name="${esc(cvName(j.company_display, j.title))}" target="_blank" rel="noopener">Download tailored CV (PDF) ↗</a>`
      : '<div class="co">Saved on your PC in the output\\resumes folder.</div>'}
    ${(j.cv_added||[]).length ? '<div class="co" style="margin-top:6px">Added (you have these): </div>' + chips(j.cv_added, 'good') : ''}
    ${(j.cv_missing||[]).length ? '<div class="co" style="margin-top:6px">Not in your CV – not added: </div>' + chips(j.cv_missing, 'warn') : ''}`;
}
/* ---------------- applied jobs: saved in the database through the local server (run_jobradar.bat / open_dashboard.bat);
   when the page is opened as a plain file, ticks are kept in this browser's storage for that file only */
const LS = 'jobradar.applied.v1';
const LOCALHOST = /^https?:$/.test(location.protocol) && ['localhost','127.0.0.1'].includes(location.hostname);
let API = false;
const APPS = new Map((D.applications||[]).map(a => [a.job_id, a]));
function lsGet(){ try { return JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch(e) { return {}; } }
function lsSet(o){ try { localStorage.setItem(LS, JSON.stringify(o)); return true; } catch(e) { return false; } }
if (!D.online && !D.cloud) for (const [id, a] of Object.entries(lsGet())) { if (a && a.removed) APPS.delete(+id); else if (a) APPS.set(+id, a); }
/* cloud: a tick opens your Telegram bot ("/start ap123"); the next scan saves it. Until then it shows as pending. */
const PEND = 'jobradar.pending.v1';
function pGet(){ try { return JSON.parse(localStorage.getItem(PEND) || '{}') || {}; } catch(e) { return {}; } }
function pSet(o){ try { localStorage.setItem(PEND, JSON.stringify(o)); } catch(e) {} }
if (D.cloud) { const o = pGet(), keep = {};
  for (const [id, p] of Object.entries(o)) { const k = +id, cur = APPS.get(k);
    if (!p || Date.now() - p.at > 6 * 3600e3) continue;
    if (p.on ? (cur && (!p.status || cur.status === p.status)) : !cur) continue;   // already saved by a scan
    keep[id] = p; if (p.on) APPS.set(k, {...(cur || p.snap || {job_id: k}), ...(p.status ? {status: p.status} : {}), pending: true}); else APPS.delete(k); }
  pSet(keep); }
const toks = s => new Set(String(s||'').toLowerCase().split(/[^a-z0-9+#]+/).filter(t => t.length > 1));
const jacc = (a, b) => { const A = toks(a), B = toks(b); if (!A.size || !B.size) return 0; let n = 0; A.forEach(t => B.has(t) && n++); return n / (A.size + B.size - n); };
const normCo = s => String(s||'').toLowerCase().replace(/\(.*?\)/g,' ').replace(/\b(llc|ltd|limited|inc|plc|pjsc|group|holding|middle east|uae)\b/g,' ').replace(/[^a-z0-9]+/g,' ').trim();
function appFor(j){
  if (APPS.has(j.id)) return {a: APPS.get(j.id), self: true};
  for (const a of APPS.values()) {
    const sameCo = (a.company_id && a.company_id === j.company_id) || (normCo(a.company) && normCo(a.company) === normCo(j.company_display));
    if (sameCo && ((a.fingerprint && a.fingerprint === j.fingerprint) || jacc(a.norm_title || a.title, j.norm_title || j.title) >= 0.85))
      return {a, self: false};
  }
  return null;
}
async function api(body){
  const r = await fetch('/api/applied', body ? {method:'POST', headers:{'Content-Type':'application/json','X-JobRadar':'1'}, body: JSON.stringify(body)} : {cache:'no-store'});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const d = await r.json(); APPS.clear(); (d.applications||[]).forEach(a => APPS.set(a.job_id, a)); return d;
}
function snapshot(j){ return {job_id: j.id, company_id: j.company_id, company: j.company_display, title: j.title, norm_title: j.norm_title,
  fingerprint: j.fingerprint, url: j.url, city: j.city, score: j.score, cv_file: j.cv_file, status: 'applied',
  applied_at: new Date().toISOString(), posting_status: 'active'}; }
async function setApplied(id, on, status){
  if (D.online) return;
  const j = D.jobs.find(x => x.id === id), prev = APPS.get(id);
  if (D.cloud) {
    const snap = prev || (j ? snapshot(j) : {job_id: id});
    if (on) APPS.set(id, {...snap, ...(status ? {status} : {}), pending: true}); else APPS.delete(id);
    const o = pGet(); o[id] = {on, status: status || null, at: Date.now(), snap}; pSet(o);
    refreshAll();
    queueTick({id, on, st: status || null});   // saved quietly through GitHub (no new tab)
    return;
  }
  if (on) APPS.set(id, {...(prev || (j ? snapshot(j) : {job_id:id})), ...(status ? {status} : {})}); else APPS.delete(id);
  if (API) { try { await api({job_id: id, applied: on, status}); } catch(e) { API = false; } }
  if (!API) { const o = lsGet(); o[id] = on ? APPS.get(id) : {removed: true}; lsSet(o); }
  refreshAll();
}
function modeNote(){
  const el = $('appmode');
  if (D.online) { el.className = 'note'; el.textContent = 'This online copy is read-only. Tick jobs as applied in the dashboard on your PC – it shows here after the next update.'; }
  else if (D.cloud) { tickPanel(el); }
  else if (API) { el.className = 'note'; el.textContent = 'Saved to your JobRadar database – kept after every scan and refresh.'; }
  else { el.className = 'note warn'; el.innerHTML = 'You opened the dashboard as a file, so ticks are kept only in this file view of this browser. For ticks that are saved in JobRadar (and stop duplicate alerts), double-click <b>open_dashboard.bat</b> and tick there.'; }
}
function tickHtml(j){
  const m = appFor(j), on = !!(m && m.self);
  const t = D.online ? 'Mark jobs as applied in the dashboard on your PC' : on ? (m.a.pending ? 'Applied (saving…) – click to undo' : 'Applied – click to undo') : 'Tick when you have applied';
  return `<button class="tick" data-tick="${j.id}" aria-pressed="${on}" title="${t}" aria-label="${t}" ${D.online ? 'disabled' : ''}>✓</button>`;
}
/* ---------------- cloud "quick ticks": an encrypted tick is sent to the 'JobRadar tick' workflow with a GitHub key
   that can only run workflows in this one repository. The key stays in this browser; the next scan applies the tick. */
const GHK = 'jobradar.ghtoken', TQ = 'jobradar.tickq';
const repoOf = () => ({owner: location.hostname.split('.')[0], repo: location.pathname.split('/').filter(Boolean)[0] || ''});
function ghToken(){ try { return localStorage.getItem(GHK) || ''; } catch(e) { return ''; } }
function tqGet(){ try { return JSON.parse(localStorage.getItem(TQ) || '[]') || []; } catch(e) { return []; } }
function tqSet(q){ try { localStorage.setItem(TQ, JSON.stringify(q)); } catch(e) {} }
function toast(msg, bad){ let t = document.getElementById('jrtoast'); if (!t) { t = document.createElement('div'); t.id = 'jrtoast';
  t.style.cssText = 'position:fixed;left:50%;bottom:20px;transform:translateX(-50%);padding:10px 16px;border-radius:8px;font:600 14px var(--body);z-index:99;box-shadow:0 4px 16px rgba(0,0,0,.25)';
  document.body.appendChild(t); }
  t.style.background = bad ? 'var(--bad)' : 'var(--good)'; t.style.color = '#fff'; t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, 3500); }
async function sealTick(items){
  let k = null; try { k = sessionStorage.getItem('jobradar.key') || localStorage.getItem('jobradar.key'); } catch(e) {}
  if (!k) throw new Error('nokey');
  const b = s => { s = s.replace(/-/g,'+').replace(/_/g,'/'); while (s.length % 4) s += '='; return Uint8Array.from(atob(s), c => c.charCodeAt(0)); };
  const ck = await crypto.subtle.importKey('raw', b(k), 'AES-GCM', false, ['encrypt']);
  const iv = crypto.getRandomValues(new Uint8Array(12)), aad = new TextEncoder().encode('JR1');
  const ct = new Uint8Array(await crypto.subtle.encrypt({name: 'AES-GCM', iv, additionalData: aad}, ck, new TextEncoder().encode(JSON.stringify(items))));
  const all = new Uint8Array(3 + 12 + ct.length); all.set(aad, 0); all.set(iv, 3); all.set(ct, 15);
  let bin = ''; all.forEach(x => bin += String.fromCharCode(x));
  return btoa(bin).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
let tqTimer = null;
function queueTick(item){
  const q = tqGet().filter(x => x.id !== item.id); q.push(item); tqSet(q);
  if (!ghToken()) { show('app'); toast('Saved on this device – connect quick ticks to save it for good', true); return; }
  clearTimeout(tqTimer); tqTimer = setTimeout(flushTicks, 1200);
}
async function flushTicks(){
  const q = tqGet(), tok = ghToken(); if (!q.length || !tok) return;
  const {owner, repo} = repoOf();
  try {
    for (let i = 0; i < q.length; i += 6) {
      const p = await sealTick(q.slice(i, i + 6));
      const r = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/applied.yml/dispatches`, {method: 'POST',
        headers: {'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'},
        body: JSON.stringify({ref: 'main', inputs: {p}})});
      if (r.status !== 204) throw new Error('HTTP ' + r.status);
    }
    tqSet([]); toast('Saved ✓');
  } catch(e) { toast(e.message === 'nokey' ? 'Open the dashboard from your private link first' : 'Could not save the tick (' + e.message + ') – check your quick-tick key', true); }
}
function tickPanel(el){
  const tok = ghToken(), q = tqGet().length;
  el.className = 'note';
  if (tok) { el.innerHTML = `Quick ticks are on in this browser ✓ – ticks save quietly and show here straight away (⏳ until the next scan confirms them).${q ? ` ${q} waiting to send.` : ''} <a href="#" id="tkoff">Turn off on this device</a>`;
    document.getElementById('tkoff').onclick = ev => { ev.preventDefault(); try { localStorage.removeItem(GHK); } catch(x) {} modeNote(); }; return; }
  el.className = 'note warn';
  el.innerHTML = `<b>Turn on quick ticks (one time, about 2 minutes, per device)</b>${q ? ` – ${q} tick(s) are waiting on this device` : ''}<br>
    1. Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub → new fine-grained token</a>.
    Name: <code>JobRadar ticks</code> · Expiration: 1 year · Repository access: <b>Only select repositories → ${esc(repoOf().repo)}</b> ·
    Permissions → Repository permissions → <b>Actions: Read and write</b> (nothing else). Click <b>Generate token</b> and copy it.<br>
    2. Paste it here (it stays in this browser only): <input id="tkin" type="password" autocomplete="off" style="width:260px;max-width:100%">
    <button class="btn" id="tksave" style="padding:5px 10px">Save</button> <span id="tkmsg"></span>`;
  document.getElementById('tksave').onclick = async () => {
    const v = document.getElementById('tkin').value.trim(), m = document.getElementById('tkmsg'), {owner, repo} = repoOf();
    if (!v) return; m.textContent = 'Checking…';
    try { const r = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/applied.yml`, {headers: {'Authorization': 'Bearer ' + v, 'Accept': 'application/vnd.github+json'}});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      try { localStorage.setItem(GHK, v); } catch(x) {} m.textContent = ''; toast('Quick ticks are on ✓'); modeNote(); flushTicks();
    } catch(e) { m.textContent = 'That key did not work (' + e.message + ') – check the repository and the Actions permission.'; }
  };
}
const open = new Set();
let tab = 'rel';
function renderJobs(){
  const q = $('q').value.trim().toLowerCase(), min = +$('min').value, inel = $('inel').checked, leads = $('leads').checked;
  const maxAge = +$('age').value, byNew = $('sort').value === 'new', hideApp = $('hideapp').checked;
  $('minv').textContent = min.toFixed(1);
  const list = D.jobs.filter(j => j.score >= min && (inel || eligible(j)) && (leads || j.is_official)
    && (!hideApp || !appFor(j))
    && (!maxAge || (postedTs(j) != null && Date.now() - postedTs(j) <= maxAge * DAY))
    && (!q || (j.title+' '+j.company_display+' '+(j.matched_skills||[]).join(' ')).toLowerCase().includes(q)));
  list.sort(byNew ? (a, b) => ((postedTs(b) ?? -1) - (postedTs(a) ?? -1)) || (b.score - a.score)
                  : (a, b) => (b.score - a.score) || ((postedTs(b) ?? -1) - (postedTs(a) ?? -1)));
  $('jshown').textContent = `${list.length} of ${D.jobs.length}`;
  $('jobs').innerHTML = list.length ? list.map(j => rowHtml(j) + (open.has(j.id) ? detailHtml(j) : '')).join('')
    : '<tr><td colspan="6" class="empty">No jobs match these filters. Lower the minimum score, or wait for the next scan.</td></tr>';
}
$('jobs').addEventListener('click', e => {
  const tk = e.target.closest('[data-tick]');
  if (tk) { e.stopPropagation(); const id = +tk.dataset.tick; setApplied(id, tk.getAttribute('aria-pressed') !== 'true'); return; }
  const tr = e.target.closest('tr.job'); if(!tr || e.target.closest('a')) return;
  const id = +tr.dataset.id; open.has(id) ? open.delete(id) : open.add(id); renderJobs(); });
$('jobs').addEventListener('keydown', e => { if (e.key==='Enter' || e.key===' ') { e.preventDefault(); e.target.closest('tr.job')?.click(); } });
['q','min','inel','leads','sort','age','hideapp'].forEach(id => $(id).addEventListener('input', renderJobs));

/* ---------------- companies */
const ATS = {greenhouse:'Greenhouse',lever:'Lever',ashby:'Ashby',smartrecruiters:'SmartRecruiters',workable:'Workable',workday:'Workday',
  oracle_hcm:'Oracle Recruiting',eightfold:'Eightfold',amazon:'amazon.jobs',successfactors:'SuccessFactors',teamtailor:'Teamtailor',pinpoint:'Pinpoint',
  jsonld:'Careers pages',phenom:'Phenom',jibe:'iCIMS Jibe',avature:'Avature',radancy:'Radancy',sitemap:'Job sitemap',html_list:'Careers page',
  rss:'RSS feed',taleo:'Taleo',json_api:'Careers-site JSON',comeet:'Comeet',whitecarrot:'WhiteCarrot',page_watch:'Careers-page watch',
  browser:'Headless browser',bamboohr:'BambooHR',manual:'LinkedIn / e-mail alerts'};
const state = s => s.last_status === 'error' ? 'fail' : s.last_status === 'unmonitored' || s.ats === 'manual' ? 'alerts' : ['ok','not_modified','partial'].includes(s.last_status) ? 'ok' : 'pending';
const SEG = [['all','All'],['ok','Live feed OK'],['fail','Failing'],['alerts','Alerts only']];
let seg = 'all';
const coCount = k => k === 'all' ? D.sources.length : D.sources.filter(s => state(s) === k).length;
function renderSeg(){ $('cseg').innerHTML = SEG.filter(([k]) => k === 'all' || coCount(k)).map(([k,l]) =>
  `<button data-k="${k}" aria-pressed="${seg===k}">${l} <span class="n">${fmt(coCount(k))}</span></button>`).join(''); }
$('cseg').addEventListener('click', e => { const b = e.target.closest('button'); if(!b) return; seg = b.dataset.k; renderSeg(); renderCos(); });
const ST = {ok:['good','✓ Live'], fail:['bad','✕ Failing'], alerts:['muted','◌ Alerts'], pending:['warn','… First run']};
function renderCos(){
  const q = $('cq').value.trim().toLowerCase();
  const list = D.sources.filter(s => (seg === 'all' || state(s) === seg) && (!q || [s.name, s.industry, ATS[s.ats]||s.ats].join(' ').toLowerCase().includes(q)))
    .sort((a,b) => ((b.best_score||0) - (a.best_score||0)) || ((b.last_job_count||0) - (a.last_job_count||0)) || a.name.localeCompare(b.name));
  $('cshown').textContent = `${list.length} of ${D.sources.length}`;
  $('cos').innerHTML = list.length ? list.map(s => { const st = ST[state(s)];
    return `<tr><td><div class="t">${s.careers_url ? `<a href="${esc(s.careers_url)}" target="_blank" rel="noopener">${esc(s.name)}</a>` : esc(s.name)}</div><div class="co">${esc(s.industry||'')}</div><div class="co">${peopleHtml(s)}</div></td>
    <td class="co">${esc(ATS[s.ats] || s.ats)}</td><td><span class="st ${st[0]}">${st[1]}</span>${state(s)==='fail' ? `<div class="co">${esc((s.last_error||'').slice(0,120))}</div>` : ''}</td>
    <td class="num mono">${state(s)==='ok' ? fmt(s.last_job_count) : '—'}</td><td class="num mono hide-sm">${s.jobs_active || '—'}</td>
    <td class="num hide-sm">${s.best_score ? `<span class="pill ${cls(s.best_score)}">${(+s.best_score).toFixed(1)}</span>` : '—'}</td>
    <td class="co hide-sm">${state(s)==='alerts' ? 'via alerts' : ago(s.last_success_at)}</td></tr>`; }).join('')
    : '<tr><td colspan="7" class="empty">No companies match.</td></tr>';
}
$('cq').addEventListener('input', renderCos);

/* ---------------- runs */
$('runs').innerHTML = D.runs.length ? D.runs.map(r => `<tr><td class="mono">${dubai(r.finished_at)}</td><td class="num mono">${r.sources_checked}</td><td class="num mono">${r.sources_failed}</td>
  <td class="num mono">${r.jobs_new}</td><td class="num mono">${r.jobs_closed}</td><td class="num mono">${r.alerts_sent}</td><td class="num mono hide-sm">${r.http_requests}</td></tr>`).join('')
  : '<tr><td colspan="7" class="empty">No scans recorded yet.</td></tr>';

/* ---------------- applied */
const STAGES = [['applied','Applied'],['interview','Interview'],['offer','Offer'],['rejected','Rejected'],['withdrawn','Withdrawn']];
function cvHref(file){ return file ? (D.cloud ? '#cv=' + file : API ? '/cv/' + encodeURIComponent(file) : D.cv_base ? D.cv_base + '/' + encodeURIComponent(file) : '') : ''; }
/* cloud: CVs are encrypted files next to the page – decrypt in the browser with the key from your link */
async function openCv(t, name){
  let k = null; try { k = sessionStorage.getItem('jobradar.key') || localStorage.getItem('jobradar.key'); } catch(e) {}
  if (!k) { alert('Open the dashboard from your private link first.'); return; }
  const b = s => { s = s.replace(/-/g,'+').replace(/_/g,'/'); while (s.length % 4) s += '='; return Uint8Array.from(atob(s), c => c.charCodeAt(0)); };
  const r = await fetch('cv/' + t + '.bin', {cache: 'no-store'}); if (!r.ok) { alert('This CV is not published yet – try after the next scan.'); return; }
  const blob = new Uint8Array(await r.arrayBuffer());
  const ck = await crypto.subtle.importKey('raw', b(k), 'AES-GCM', false, ['decrypt']);
  const pdf = await crypto.subtle.decrypt({name: 'AES-GCM', iv: blob.slice(3, 15), additionalData: new TextEncoder().encode('JR1')}, ck, blob.slice(15));
  const url = URL.createObjectURL(new Blob([pdf], {type: 'application/pdf'}));
  const a = document.createElement('a'); a.href = url; a.download = name || 'Tailored_CV.pdf'; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
const cvName = (co, title) => ('Tailored_CV_' + String(co||'') + '_' + String(title||'')).replace(/\(.*?\)/g, '').replace(/[^A-Za-z0-9]+/g, '_').slice(0, 90) + '.pdf';
document.addEventListener('click', e => { const a = e.target.closest('a[href^="#cv="]'); if (!a) return;
  e.preventDefault(); e.stopPropagation(); openCv(a.getAttribute('href').slice(4), a.dataset.name).catch(() => alert('Could not open the CV.')); }, true);
function renderApps(){
  modeNote();
  const list = [...APPS.values()].sort((a,b) => String(b.applied_at||'').localeCompare(String(a.applied_at||'')));
  $('apps').innerHTML = list.length ? list.map(a => {
    const sc = a.current_score ?? a.score, cv = cvHref(a.cv_file);
    const live = a.posting_status === 'closed' ? '<span class="st bad">Closed</span>' : '<span class="st ok">Still open</span>';
    return `<tr><td class="mono co">${dubai(a.applied_at)}</td>
      <td><div class="t">${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title)}</div><div class="co">${esc(a.company)}</div>${a.pending ? '<span class="chip warn">⏳ confirmed at the next scan</span>' : ''}
        <div class="co">${peopleHtml({company_display: a.company})}</div></td>
      <td class="num">${sc != null ? `<span class="pill ${cls(sc)}">${(+sc).toFixed(1)}</span>` : '—'}</td>
      <td><select data-stage="${a.job_id}" ${D.online ? 'disabled' : ''} aria-label="Stage">${STAGES.map(([k,l]) => `<option value="${k}" ${a.status===k?'selected':''}>${l}</option>`).join('')}</select></td>
      <td class="hide-sm">${live}</td>
      <td class="hide-sm co">${cv ? `<a href="${esc(cv)}" data-name="${esc(cvName(a.company, a.title))}" target="_blank" rel="noopener">Tailored CV ↗</a>` : '—'}</td>
      <td><button class="tick" data-untick="${a.job_id}" aria-pressed="true" title="Untick to move back" aria-label="Applied – untick to move back" ${D.online ? 'disabled' : ''}>✓</button></td></tr>`; }).join('')
    : '<tr><td colspan="7" class="empty">Nothing here yet. Tick the ✓ box next to a job after you apply and it moves here.</td></tr>';
}
$('apps').addEventListener('click', e => { const b = e.target.closest('[data-untick]'); if (b) setApplied(+b.dataset.untick, false); });
$('apps').addEventListener('change', e => { const s = e.target.closest('[data-stage]'); if (s) setApplied(+s.dataset.stage, true, s.value); });
function counts(){
  $('n-app').textContent = fmt(APPS.size);
  $('n-rel').textContent = fmt(relevant.filter(j => !appFor(j)).length);
}
function refreshAll(){ counts(); if (tab === 'app') renderApps(); else if (tab !== 'co' && tab !== 'log') renderJobs(); }

/* ---------------- tabs */
$('n-new').textContent = fmt(D.jobs.filter(j => eligible(j) && postedTs(j) != null && Date.now() - postedTs(j) <= 7 * DAY).length) + ' this week';
$('n-rel').textContent = fmt(relevant.length); $('n-all').textContent = fmt(D.jobs.length); $('n-co').textContent = fmt(D.sources.length);
function show(t){ tab = t;
  document.querySelectorAll('[role=tab]').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === t));
  $('tab-jobs').hidden = !(t === 'rel' || t === 'all' || t === 'new'); $('tab-co').hidden = t !== 'co'; $('tab-log').hidden = t !== 'log';
  $('tab-app').hidden = t !== 'app'; if (t === 'app') renderApps();
  if (t === 'rel') { $('min').value = thr; $('inel').checked = false; $('sort').value = 'new'; $('age').value = '0'; }
  if (t === 'new') { $('min').value = 0; $('inel').checked = false; $('sort').value = 'new'; $('age').value = '30'; }
  if (t === 'all') { $('min').value = 0; $('inel').checked = true; $('sort').value = 'score'; $('age').value = '0'; }
  if (t === 'rel' || t === 'all' || t === 'new') renderJobs(); if (t === 'co') { renderSeg(); renderCos(); } }
document.querySelector('.tabs').addEventListener('click', e => { const b = e.target.closest('[role=tab]'); if (b) show(b.dataset.tab); });
show('rel'); counts();
if (D.cloud && ghToken() && tqGet().length) flushTicks();
if (D.cloud) { const p = document.createElement('p'); p.className = 'foot';
  p.innerHTML = '<a href="#" id="lock">Lock this device</a> – forgets your key in this browser; open your private link to unlock again.';
  $('tab-log').parentNode.appendChild(p);
  $('lock').addEventListener('click', e => { e.preventDefault(); try { localStorage.removeItem('jobradar.key'); sessionStorage.removeItem('jobradar.key'); } catch(x) {}
    location.replace(location.pathname); }); }
if (LOCALHOST && !D.online && !D.cloud) api().then(async () => {   // server is running: copy browser-only ticks into the database
  API = true; const o = lsGet(), items = Object.entries(o).map(([id, a]) => a && a.removed ? {job_id: +id, applied: false} : {job_id: +id, applied: true, status: a && a.status});
  if (items.length) { try { await api({items}); lsSet({}); } catch(e) {} }
  refreshAll(); if (tab === 'app') renderApps();
}).catch(() => { API = false; });
})();
</script>
"""


def render(data: dict, fragment: bool = False, mode: str | None = None, cloud: dict | None = None) -> str:
    if mode == "cloud":  # encrypted GitHub Pages copy: CV names → opaque tokens, ticks go through Telegram
        tok = (cloud or {}).get("cv") or {}
        data = {**data, "cv_base": None, "cloud": True, "bot": (cloud or {}).get("bot"),
                "jobs": [{**j, "cv_file": tok.get(j.get("cv_file"))} for j in data["jobs"]],
                "applications": [{**a, "cv_file": tok.get(a.get("cv_file"))} for a in data.get("applications", [])]}
    if fragment:  # the online copy never links to (or embeds) your CV files – they stay on your PC
        data = {**data, "cv_base": None, "online": True, "jobs": [{**j, "cv_file": None} for j in data["jobs"]],
                "applications": [{**a, "cv_file": None, "note": None} for a in data.get("applications", [])]}
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    body = BODY.replace("__DATA__", payload)
    if fragment:  # for Artifact publishing (skeleton is added by the host)
        return STYLE + body
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            + STYLE + "</head><body>" + body + "</body></html>")


def build_dashboard(settings, db: Database, fragment_path: str | None = None) -> str:
    import os
    from .config import ROOT
    data = dashboard_data(db, settings)
    out_dir = Path(settings.raw["paths"]["dashboard_dir"])
    if not out_dir.is_absolute():
        out_dir = settings.path("dashboard_dir")
    cv_dir = ROOT / settings.get("resume", "output_dir", "output/resumes")
    data["cv_base"] = os.path.relpath(cv_dir, out_dir).replace("\\", "/")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data["stats"], indent=2, default=str), encoding="utf-8")
    if fragment_path:
        Path(fragment_path).write_text(render(data, fragment=True), encoding="utf-8")
    return str(out_dir / "index.html")

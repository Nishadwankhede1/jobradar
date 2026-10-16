"""Cloud mode (GitHub Actions + GitHub Pages, public repository, everything personal encrypted).

    python -m jobradar cloud-restore vault.bin   # decrypt state/ + output/ before a scan
    python -m jobradar cloud-save vault.bin      # encrypt them again afterwards
    python -m jobradar cloud-publish pages/      # encrypted dashboard + CVs for GitHub Pages

Key: JOBRADAR_KEY (GitHub secret). The published site holds only ciphertext; your private link carries the key in
the part after '#', which browsers never send to any server.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

from .config import ROOT
from .crypto import decrypt, encrypt, parse_key, token

VAULT_PATHS = ("state", "output")
SKIP = {"outbox.log"}


def vault_save(out: Path, root: Path = ROOT) -> int:
    key = parse_key()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for p in VAULT_PATHS:
            d = root / p
            if d.exists():
                tar.add(d, arcname=p, filter=lambda ti: None if Path(ti.name).name in SKIP or "__pycache__" in ti.name else ti)
    blob = encrypt(buf.getvalue(), key)
    Path(out).write_bytes(blob)
    return len(blob)


def vault_restore(src: Path, root: Path = ROOT) -> bool:
    src = Path(src)
    if not src.is_file():
        return False
    data = decrypt(src.read_bytes(), parse_key())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():  # only our own two folders, no absolute paths or '..'
            if m.name.split("/")[0] not in VAULT_PATHS or ".." in Path(m.name).parts or m.name.startswith("/") or m.issym() or m.islnk():
                raise ValueError(f"unexpected entry in vault: {m.name}")
        tar.extractall(root)
    return True


SHELL = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer"><title>JobRadar</title>
<style>
:root{--bg:#f3f5f4;--fg:#14201f;--muted:#5a6967;--line:#d3dad8;--accent:#0d6e6a;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0e1514;--fg:#e3ecea;--muted:#93a5a2;--line:#2b3a38;--accent:#4fc1b8;--bad:#f08a80}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
main{max-width:420px;width:100%}h1{font-size:28px;margin:0 0 6px}p{color:var(--muted);margin:0 0 16px}
input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit}
button{margin-top:10px;font:600 15px system-ui;padding:10px 14px;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}
.err{color:var(--bad)}
</style></head><body><main>
<h1>JobRadar</h1><p id="msg">Opening your dashboard…</p>
<form id="f" hidden><input id="k" autocomplete="off" placeholder="Paste your dashboard key" aria-label="Dashboard key"><button>Open</button></form>
</main><script>
(async function(){
  const LS='jobradar.key', msg=document.getElementById('msg'), f=document.getElementById('f');
  const b64=s=>{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';return Uint8Array.from(atob(s),c=>c.charCodeAt(0));};
  function fromHash(){const m=location.hash.match(/k=([A-Za-z0-9_-]{40,})/);return m?m[1]:null;}
  let key=fromHash(); if(!key){try{key=localStorage.getItem(LS);}catch(e){}}
  async function open(k){
    const r=await fetch('data.bin?'+Date.now(),{cache:'no-store'}); if(!r.ok) throw new Error('fetch');
    const blob=new Uint8Array(await r.arrayBuffer());
    const ck=await crypto.subtle.importKey('raw',b64(k),'AES-GCM',false,['decrypt']);
    const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:blob.slice(3,15),additionalData:new TextEncoder().encode('JR1')},ck,blob.slice(15));
    try{localStorage.setItem(LS,k);}catch(e){} try{sessionStorage.setItem(LS,k);}catch(e){}
    const html=new TextDecoder().decode(pt); document.open(); document.write(html); document.close();
  }
  function ask(t){msg.textContent=t;msg.className=t.startsWith('This')?'err':'';f.hidden=false;}
  f.addEventListener('submit',e=>{e.preventDefault();const v=document.getElementById('k').value.trim().replace(/^.*k=/,'');
    open(v).catch(()=>ask('This key does not open the dashboard. Check your private link.'));});
  if(!key) return ask('Open your private dashboard link, or paste your key.');
  open(key).catch(err=>ask(err.message==='fetch'?'The dashboard has not been published yet – wait for the first scan.':'This key does not open the dashboard. Check your private link.'));
})();
</script></body></html>"""


def apply_ticks(runs_json: Path, db) -> int:
    """Ticks sent from the dashboard arrive as titles of 'JobRadar tick' runs: "tick <base64url(JR1 ciphertext)>".
    Each payload is a JSON list of {"id": job_id, "on": bool, "st": stage} (applied ticks) or {"sk": keyword, "on": bool}
    (skills you confirmed for your CVs). Only payloads that decrypt with your key
    are accepted (nobody else can forge one), and each run is applied once."""
    import base64
    from .applications import STATUSES, set_applied
    from .region import REGION
    key = parse_key()
    runs = (json.loads(Path(runs_json).read_text(encoding="utf-8")) or {}).get("workflow_runs") or []
    done = set(db.kv_get("ticks_done", []) or [])
    n = 0
    for run in sorted(runs, key=lambda r: r.get("created_at") or ""):
        rid, title = str(run.get("id")), run.get("display_title") or ""
        if rid in done or not title.startswith("tick "):
            continue
        done.add(rid)
        try:
            raw = title[5:].strip()
            items = json.loads(decrypt(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), key))
        except Exception:
            continue  # not ours / damaged → ignore
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("sk"):  # "I know this skill" from a CV keyword chip
                from .resume.myskills import set_user_skill
                n += bool(set_user_skill(db, str(it["sk"]), bool(it.get("on", True))))
                continue
            if (it.get("r") or "uae") != REGION:  # an Applied tick from the other dashboard (UAE ↔ Europe)
                continue
            try:
                st = it.get("st") if it.get("st") in STATUSES else None
                set_applied(db, int(it["id"]), bool(it.get("on", True)), st)
                n += 1
            except (KeyError, ValueError, TypeError):
                continue
    db.kv_set("ticks_done", sorted(done, key=int)[-1000:])
    db.commit()
    return n


def publish(out_dir: Path, settings, db, bot: str | None = None) -> dict:
    """Write the encrypted site: index.html (opener), data.bin (dashboard), cv/<token>.bin (tailored CVs)."""
    from .dashboard import dashboard_data, render
    key = parse_key()
    out = Path(out_dir)
    (out / "cv").mkdir(parents=True, exist_ok=True)
    data = dashboard_data(db, settings)
    cv_dir = ROOT / settings.get("resume", "output_dir", "output/resumes")
    tokens = {}
    wanted = {r["file"] for r in db.q("SELECT file FROM resumes")} | \
        {r["cv_file"] for r in db.q("SELECT cv_file FROM applications WHERE cv_file IS NOT NULL")}
    for f in sorted(cv_dir.glob("*.pdf")) if cv_dir.exists() else []:
        if f.name not in wanted:
            continue
        t = token(f.name, key)
        tokens[f.name] = t
        (out / "cv" / f"{t}.bin").write_bytes(encrypt(f.read_bytes(), key))
    # optional TICK_TOKEN secret (fine-grained, Actions-only on this repo): travels inside the encrypted page so every
    # device that opens your private link can save ticks without its own setup
    gh = (os.environ.get("JOBRADAR_TICK_TOKEN") or "").strip() or None
    html = render(data, mode="cloud", cloud={"bot": bot, "cv": tokens, "gh": gh})
    (out / "data.bin").write_bytes(encrypt(html.encode("utf-8"), key))
    (out / "index.html").write_text(SHELL, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return {"cvs": len(tokens), "dashboard_bytes": len(html)}

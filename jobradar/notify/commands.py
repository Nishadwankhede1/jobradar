"""Telegram commands, read at the start of every cloud scan (no server needed).

From the alert message or the dashboard you can send:
    /applied_123          mark job 123 as applied      (dashboard ✓ sends "/start ap123")
    /unapply_123          move it back                 ("/start un123")
    /stage_123_interview  applied | interview | offer | rejected | withdrawn   ("/start st123_interview")
    /applied              list your recent applications
Only messages from TELEGRAM_CHAT_ID (your own chat with the bot) are accepted; anything else is ignored.
"""
from __future__ import annotations

import json
import os
import re

from ..applications import STATUSES, all_applications, set_applied
from ..util import log

CMD = re.compile(r"^/(?:start\s+)?(ap|applied|un|unapply|st|stage)[_\s-]?(\d+)(?:[_\s-]+([a-z]+))?\s*$", re.I)
HELP = ("JobRadar is connected.\n\nTap /applied_<id> under an alert (or ✓ on the dashboard) after you apply – the job "
        "moves to your Applied list and you won't be alerted about it again.\n/applied – your recent applications")


def _reply(http, token, chat, text):
    try:
        http.post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       json_body={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
    except Exception as e:  # a failed confirmation must not stop the scan
        log.warning(f"telegram reply failed: {type(e).__name__}")


def handle_text(db, text: str) -> str | None:
    """Apply one command; returns the confirmation text (None = not a JobRadar command)."""
    text = (text or "").strip()
    if text.lower() in ("/start", "/help"):
        return HELP
    if text.lower() in ("/applied", "/list"):
        apps = all_applications(db)[:15]
        if not apps:
            return "No applications yet. Tap /applied_<id> under an alert after you apply."
        return "Your applications:\n" + "\n".join(f"• {a['title']} – {a['company']} ({a['status']}, {str(a['applied_at'])[:10]})"
                                                for a in apps)
    m = CMD.match(text)
    if not m:
        return None
    verb, jid, arg = m.group(1).lower(), int(m.group(2)), (m.group(3) or "").lower()
    job = db.one("SELECT title, company_display FROM jobs WHERE id=?", (jid,))
    if not job:
        return f"Job {jid} isn't in JobRadar any more."
    label = f"{job['title']} – {job['company_display']}"
    if verb in ("un", "unapply"):
        set_applied(db, jid, False)
        return f"↩ Moved back to your job lists: {label}"
    if verb in ("st", "stage"):
        if arg not in STATUSES:
            return "Stage must be one of: " + ", ".join(STATUSES)
        set_applied(db, jid, True, arg)
        return f"Stage set to {arg}: {label}"
    set_applied(db, jid, True)
    return f"✅ Marked as applied: {label}\nYou won't get alerts for this role again."


def process_commands(db, http) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return 0
    offset = int(db.kv_get("telegram_offset", 0) or 0)
    try:
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset + 1
        updates = http.get_json(f"https://api.telegram.org/bot{token}/getUpdates", params=params).get("result") or []
    except Exception as e:
        log.warning(f"telegram commands skipped: {type(e).__name__}")
        return 0
    n = 0
    for u in updates:
        offset = max(offset, int(u.get("update_id") or 0))
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue  # only you can change your data
        reply = handle_text(db, msg.get("text") or "")
        if reply:
            n += 1
            _reply(http, token, chat, reply)
    db.kv_set("telegram_offset", offset)
    db.commit()
    return n


def bot_username(http) -> str | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        return (http.get_json(f"https://api.telegram.org/bot{token}/getMe").get("result") or {}).get("username")
    except Exception:
        return None

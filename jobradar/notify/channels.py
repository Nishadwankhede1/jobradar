"""Delivery channels. Telegram is primary; WhatsApp (Meta Cloud API or Twilio) is optional."""
from __future__ import annotations

import base64
import os
import time
import urllib.parse
from pathlib import Path

from ..http import HttpClient, HttpError
from ..util import log, now_iso


class Channel:
    name = "base"

    def configured(self) -> bool:
        return False

    def send(self, text_html: str, text_plain: str) -> str:  # returns provider message id
        raise NotImplementedError


class Telegram(Channel):
    """Telegram Bot API. Env: TELEGRAM_BOT_TOKEN (from @BotFather), TELEGRAM_CHAT_ID (see `jobradar telegram-setup`)."""
    name = "telegram"
    LIMIT = 4000

    def __init__(self, http: HttpClient):
        self.http = http
        self.token, self.chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

    def configured(self):
        return bool(self.token and self.chat_id)

    def send(self, text_html, text_plain):
        chunks = [text_html[i:i + self.LIMIT] for i in range(0, len(text_html), self.LIMIT)] or [""]
        mid = ""
        for chunk in chunks:
            for attempt in range(2):
                try:
                    r = self.http.post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", json_body={
                        "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True})
                    mid = str((r.get("result") or {}).get("message_id", ""))
                    break
                except HttpError as e:
                    if e.status == 400 and attempt == 0:  # HTML parse problem → resend as plain text
                        r = self.http.post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", json_body={
                            "chat_id": self.chat_id, "text": text_plain[: self.LIMIT], "disable_web_page_preview": True})
                        mid = str((r.get("result") or {}).get("message_id", ""))
                        return mid
                    if attempt == 1:
                        raise
                    time.sleep(2 + attempt * 3)
        return mid

    def send_document(self, path: Path, caption: str = "") -> str:
        """Upload a file (the tailored CV) to your chat."""
        import secrets as _s
        b = "----jobradar" + _s.token_hex(8)
        parts = []
        for k, v in (("chat_id", str(self.chat_id)), ("caption", caption[:1000])):
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{Path(path).name}"\r\n'
                     f'Content-Type: application/pdf\r\n\r\n'.encode() + Path(path).read_bytes() + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=b"".join(parts),
                           headers={"Content-Type": f"multipart/form-data; boundary={b}"}).json()
        return str((r.get("result") or {}).get("message_id", ""))

    def get_updates(self) -> list[dict]:
        r = self.http.get_json(f"https://api.telegram.org/bot{self.token}/getUpdates")
        return r.get("result", [])


class WhatsApp(Channel):
    """Optional. Either Meta WhatsApp Cloud API (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TO)
    or Twilio (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, WHATSAPP_TO).
    Note: Meta only allows free-form business-initiated messages inside a 24h customer window; outside it a
    pre-approved template is required (set WHATSAPP_TEMPLATE). Twilio sandbox works for personal testing."""
    name = "whatsapp"

    def __init__(self, http: HttpClient):
        self.http = http
        self.to = os.environ.get("WHATSAPP_TO")

    def configured(self):
        meta = os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        twilio = os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN") and os.environ.get("TWILIO_WHATSAPP_FROM")
        return bool(self.to and (meta or twilio))

    def send(self, text_html, text_plain):
        body = text_plain[:1500]
        if os.environ.get("WHATSAPP_TOKEN"):
            pid = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
            tmpl = os.environ.get("WHATSAPP_TEMPLATE")
            payload = ({"messaging_product": "whatsapp", "to": self.to, "type": "template",
                        "template": {"name": tmpl, "language": {"code": "en"},
                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": body[:1000]}]}]}}
                       if tmpl else {"messaging_product": "whatsapp", "to": self.to, "type": "text", "text": {"body": body}})
            r = self.http.post_json(f"https://graph.facebook.com/v20.0/{pid}/messages",
                                    headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}, json_body=payload)
            return str(((r.get("messages") or [{}])[0]).get("id", ""))
        sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        data = urllib.parse.urlencode({"From": f"whatsapp:{os.environ['TWILIO_WHATSAPP_FROM']}", "To": f"whatsapp:{self.to}",
                                       "Body": body}).encode()
        r = self.http.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data,
                           headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        return str(r.json().get("sid", ""))


class Outbox(Channel):
    """Dry-run channel: writes messages to state/outbox.log (used by tests and `--dry-run`)."""
    name = "outbox"

    def __init__(self, path: Path):
        self.path = path
        self.sent: list[str] = []

    def configured(self):
        return True

    def send(self, text_html, text_plain):
        self.sent.append(text_plain)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"----- {now_iso()}\n{text_plain}\n")
        return f"outbox-{len(self.sent)}"


def build_channels(settings, http: HttpClient, dry_run: bool = False, root: Path | None = None) -> list[Channel]:
    if dry_run:
        return [Outbox((root or Path(".")) / "state" / "outbox.log")]
    out = []
    for name in settings.get("notify", "channels", ["telegram"]):
        ch = Telegram(http) if name == "telegram" else WhatsApp(http) if name == "whatsapp" else None
        if ch and ch.configured():
            out.append(ch)
        elif ch:
            log.warning("notification channel '%s' is enabled but not configured (missing env vars)", name)
    return out

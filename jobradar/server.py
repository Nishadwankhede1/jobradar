"""Local dashboard server (http://localhost:8765) – lets the dashboard save "Applied" ticks into the database.

Listens on 127.0.0.1 only. Write requests must carry the X-JobRadar header, which a browser will not send from
another website without a CORS pre-flight this server never approves – so no other site can change your data.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from .applications import STATUSES, all_applications, set_applied
from .config import ROOT, load_settings
from .db import Database


class Handler(http.server.SimpleHTTPRequestHandler):
    settings = None
    cv_dir: Path = ROOT / "output" / "resumes"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _db(self) -> Database:
        return Database(self.settings.path("database"))

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/applied":
            db = self._db()
            try:
                return self._json(200, {"ok": True, "applications": all_applications(db)})
            finally:
                db.close()
        if path.startswith("/cv/"):
            name = os.path.basename(unquote(path[4:]))
            f = self.cv_dir / name
            if not name.lower().endswith(".pdf") or not f.is_file():
                return self._json(404, {"ok": False, "error": "CV not found – run the scan to create it"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/applied":
            return self._json(404, {"ok": False})
        if self.headers.get("X-JobRadar") != "1" or not self._local_host():
            return self._json(403, {"ok": False, "error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
            items = req.get("items") if isinstance(req.get("items"), list) else [req]
        except (ValueError, TypeError):
            return self._json(400, {"ok": False, "error": "bad request"})
        db = self._db()
        try:
            done = []
            for it in items[:500]:
                status = it.get("status") if it.get("status") in STATUSES else None
                try:
                    done.append(set_applied(db, int(it["job_id"]), bool(it.get("applied", True)), status,
                                            (it.get("note") or None) and str(it["note"])[:500]))
                except (KeyError, ValueError, TypeError):
                    continue
            return self._json(200, {"ok": True, "saved": done, "applications": all_applications(db)})
        finally:
            db.close()


def serve(port: int = 8765, open_browser: bool = False) -> None:
    import webbrowser
    settings = load_settings()
    Handler.settings = settings
    Handler.cv_dir = ROOT / settings.get("resume", "output_dir", "output/resumes")
    handler = functools.partial(Handler, directory=str(settings.path("dashboard_dir")))
    url = f"http://localhost:{port}/"
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:  # already running in another window → just open it
        print(f"The dashboard is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Dashboard: {url}")
    print("Keep this window open while you use the dashboard (ticks are saved through it). Close it when you are done.")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

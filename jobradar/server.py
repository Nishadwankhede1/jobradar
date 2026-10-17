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
        path = urlparse(self.path).path
        if path not in ("/api/applied", "/api/skill", "/api/notrelevant"):
            return self._json(404, {"ok": False})
        if self.headers.get("X-JobRadar") != "1" or not self._local_host():
            return self._json(403, {"ok": False, "error": "forbidden"})
        if path == "/api/skill":
            return self._skill()
        if path == "/api/notrelevant":
            return self._not_relevant()
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


    def _not_relevant(self):
        """{"job_id": 123, "on": true} → hide the job (and its reposts) from the dashboard and from alerts."""
        from .applications import not_relevant, set_not_relevant
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 2000)) or b"{}")
            job_id, on = int(req["job_id"]), bool(req.get("on", True))
        except (KeyError, ValueError, TypeError):
            return self._json(400, {"ok": False, "error": "bad request"})
        db = self._db()
        try:
            try:
                set_not_relevant(db, job_id, on)
            except KeyError:
                return self._json(404, {"ok": False, "error": "job not found"})
            return self._json(200, {"ok": True, "not_relevant": not_relevant(db)})
        finally:
            db.close()

    def _skill(self):
        """{"term": "Airflow", "on": true, "job_id": 123} → remember the skill and rebuild that job's CV right away
        (the other CVs are refreshed at the next scan)."""
        from .resume.myskills import get_user_skills, set_user_skill
        from .resume.service import build_resumes
        from .config import ROOT
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 2000)) or b"{}")
            term, on = str(req.get("term") or "").strip(), bool(req.get("on", True))
            job_id = int(req["job_id"]) if req.get("job_id") is not None else None
        except (ValueError, TypeError):
            return self._json(400, {"ok": False, "error": "bad request"})
        if not term:
            return self._json(400, {"ok": False, "error": "no keyword"})
        db = self._db()
        try:
            set_user_skill(db, term, on)
            cv = None
            if job_id is not None:
                build_resumes(db, self.settings, ROOT, job_ids=[job_id])
                r = db.q("SELECT * FROM resumes WHERE job_id=?", (job_id,))
                if r:
                    r = r[0]
                    cv = {"cv_file": r["file"], "cv_before": r["coverage_before"], "cv_after": r["coverage_after"],
                          "cv_added": json.loads(r["added"]) if isinstance(r["added"], str) else r["added"],
                          "cv_missing": json.loads(r["missing"]) if isinstance(r["missing"], str) else r["missing"]}
            return self._json(200, {"ok": True, "user_skills": [x["term"] for x in get_user_skills(db)], "cv": cv})
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

"""HTTP server for the portal (standard library only, 127.0.0.1 only).

GET  /                  the portal page
GET  /api/all           everything the page shows, in one call
GET  /api/logs?name=    the last lines of the scanner or study log
GET  /api/ping          liveness check (used by the .exe launcher)
POST /api/start/<name>  start the scanner or study      } need the per-launch
POST /api/stop/<name>   stop it                         } token header, so no
POST /api/export        write CSVs into .\\exports      } web page can press
POST /api/open-exports  open the exports folder         } these buttons
POST /api/quit          stop everything and close       }

Requests whose Host header isn't 127.0.0.1/localhost are refused (blocks DNS
rebinding: another site can't read this page's data through a renamed host).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import data
from .procs import ProcessManager

PAGE = Path(__file__).with_name("page.html")


class Portal:
    def __init__(self, cfg: dict, root: Path, procs: ProcessManager | None = None):
        self.cfg = cfg
        self.root = root
        self.db = cfg["db_path"]
        self.procs = procs or ProcessManager(root, root / "logs")
        self.token = secrets.token_urlsafe(24)
        self.exports = root / "exports"
        self.on_quit = None            # set by __main__: stops the processes and the server

    def all(self) -> dict:
        return {"processes": self.procs.status(), "summary": data.summary(self.db),
                "positions": data.positions(self.db), "trades": data.trades(self.db),
                "signals": data.signals(self.db), "equity": data.equity(self.db)}

    def export(self) -> dict:
        from ..export import export
        counts = export(self.db, self.exports, self.cfg)
        return {"folder": str(self.exports), "files": counts}

    def open_exports(self) -> dict:
        self.exports.mkdir(exist_ok=True)
        if os.name == "nt":
            os.startfile(self.exports)  # noqa: S606 - opens Explorer on the user's own folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(self.exports)])
        return {"folder": str(self.exports)}


def make_handler(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):      # keep the console quiet
            pass

        def _send(self, code: int, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host in ("127.0.0.1", "localhost"):
                return True
            self._send(403, {"error": "portal answers on 127.0.0.1 only"})
            return False

        def do_GET(self):
            if not self._local():
                return
            u = urlparse(self.path)
            if u.path == "/":
                html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", portal.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/ping":
                return self._send(200, {"ok": True, "app": "solpaper-portal"})
            if u.path == "/api/all":
                return self._send(200, portal.all())
            if u.path == "/api/logs":
                name = parse_qs(u.query).get("name", ["scanner"])[0]
                if name not in portal.procs.commands:
                    return self._send(404, {"error": "unknown log"})
                return self._send(200, {"lines": data.tail(portal.procs.log_path(name))})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return
            if self.headers.get("X-Portal-Token") != portal.token:
                return self._send(403, {"error": "missing or wrong portal token"})
            parts = urlparse(self.path).path.strip("/").split("/")
            try:
                if parts[:2] == ["api", "start"] and len(parts) == 3:
                    return self._send(200, portal.procs.start(parts[2]))
                if parts[:2] == ["api", "stop"] and len(parts) == 3:
                    return self._send(200, portal.procs.stop(parts[2]))
                if parts == ["api", "export"]:
                    return self._send(200, portal.export())
                if parts == ["api", "open-exports"]:
                    return self._send(200, portal.open_exports())
                if parts == ["api", "quit"]:
                    self._send(200, {"ok": True})
                    if portal.on_quit:
                        portal.on_quit()
                    return
            except KeyError:
                return self._send(404, {"error": "unknown process"})
            self._send(404, {"error": "not found"})

    return Handler


class LocalServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR would let a second portal share a port that's in use.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(portal: Portal, port: int, tries: int = 1) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port, or the next free port if tries > 1."""
    for p in range(port, port + max(1, tries)):
        try:
            return LocalServer(("127.0.0.1", p), make_handler(portal))
        except OSError:
            if p == port + max(1, tries) - 1:
                raise

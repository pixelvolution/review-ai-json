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
POST /api/update        git pull (+ pip install if the  }
                        requirements changed)           }
POST /api/restart       restart onto the pulled code    }
POST /api/reset         start a new wallet run (only    }
                        after a cook)                   }

Requests whose Host header isn't 127.0.0.1/localhost are refused (blocks DNS
rebinding: another site can't read this page's data through a renamed host).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import data
from ..wallet import WalletError
from .procs import ProcessManager

PAGE = Path(__file__).with_name("page.html")
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Portal:
    def __init__(self, cfg: dict, root: Path, procs: ProcessManager | None = None):
        self.cfg = cfg
        self.root = root
        self.db = cfg["db_path"]
        self.procs = procs or ProcessManager(root, root / "logs")
        self.token = secrets.token_urlsafe(24)
        self.exports = root / "exports"
        self.on_quit = None            # set by __main__: stops the processes and the server
        self.on_restart = None         # set by __main__: hands over to a fresh portal process
        self.boot = secrets.token_hex(4)   # changes on every start; the page reloads on a new one
        self._lock = threading.Lock()      # one update/reset at a time
        self.version = self.head()         # the code this portal (and its processes) started on

    def head(self) -> str | None:
        try:
            r = self.git("rev-parse", "HEAD", timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def git(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)

    def update(self) -> dict:
        """git pull (fast-forward only). Never touches the database or local settings."""
        with self._lock:
            try:
                before = self.git("rev-parse", "HEAD")
            except FileNotFoundError:
                return {"ok": False, "output": "git isn't installed or isn't on PATH. "
                        "Pull in GitHub Desktop instead, then press Update again to restart."}
            if before.returncode:
                return {"ok": False, "output": before.stderr.strip() or "not a git repository"}
            branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            try:
                pull = self.git("pull", "--ff-only")
            except subprocess.TimeoutExpired:
                return {"ok": False, "branch": branch, "output": "git pull timed out (2 min)."}
            out = (pull.stdout + pull.stderr).strip()
            after = self.git("rev-parse", "HEAD").stdout.strip()
            changed = after != before.stdout.strip()
            res = {"ok": pull.returncode == 0, "branch": branch, "changed": changed, "output": out,
                   # e.g. pulled in GitHub Desktop: nothing new to pull, but what's running is old
                   "restart_needed": bool(self.version) and after != self.version}
            if changed:
                files = self.git("diff", "--name-only", before.stdout.strip(), after).stdout.split()
                res["commits"] = self.git("log", "--oneline", f"{before.stdout.strip()}..{after}"
                                          ).stdout.strip().splitlines()
                if "solpaper/requirements.txt" in files:
                    pip = subprocess.run([self.procs.python, "-m", "pip", "install", "-r",
                                          "solpaper/requirements.txt"], cwd=self.root,
                                         capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", timeout=600, **NO_WINDOW)
                    res["output"] += "\n\n[packages] " + ("installed" if pip.returncode == 0
                                                          else pip.stdout[-1500:] + pip.stderr[-1500:])
            return res

    def reset_wallet(self) -> dict:
        """Start run N+1 after a cook. The scanner caches the run it started with, so a
        running scanner is restarted to pick up the new run."""
        from ..reset import reset, wallet_config
        with self._lock:
            was_running = self.procs.running("scanner")
            if was_running:
                self.procs.stop("scanner")
            try:
                run = reset(self.db, wallet_config(self.cfg))
            finally:
                if was_running:
                    self.procs.start("scanner")
            return {"run": run, "start_balance": self.cfg["wallet"]["start_balance"],
                    "scanner_restarted": was_running}

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
                return self._send(200, {"ok": True, "app": "solpaper-portal", "boot": portal.boot,
                                        "version": (portal.version or "")[:7]})
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
                if parts == ["api", "update"]:
                    return self._send(200, portal.update())
                if parts == ["api", "reset"]:
                    return self._send(200, portal.reset_wallet())
                if parts == ["api", "restart"]:
                    if not portal.on_restart:
                        return self._send(409, {"error": "restart isn't available here"})
                    self._send(200, {"ok": True, "boot": portal.boot,
                                        "version": (portal.version or "")[:7]})
                    portal.on_restart()
                    return
                if parts == ["api", "quit"]:
                    self._send(200, {"ok": True})
                    if portal.on_quit:
                        portal.on_quit()
                    return
            except KeyError:
                return self._send(404, {"error": "unknown process"})
            except WalletError as e:            # e.g. reset while the run is still alive
                return self._send(409, {"error": str(e)})
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

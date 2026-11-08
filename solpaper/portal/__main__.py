"""Run the portal: ``python -m solpaper.portal [--autostart] [--port 8765] [--no-browser]``.

``--start scanner,study`` starts just those; ``--wait-port`` (used by Restart
after an Update) waits for the old portal to let go of the port first.

Opens a local page (http://127.0.0.1:<port>) with start/stop buttons for the
scanner and the study, the mock wallet, trades, signals, filters and logs.
Closing this process stops the scanner and study it started.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import urllib.request
import webbrowser
from pathlib import Path

from ..config import load_config
from .server import Portal, serve

DEFAULT_PORT = 8765
ROOT = Path(__file__).resolve().parents[2]     # the repo folder (holds solpaper/)
PORT_FILE = ROOT / "logs" / "portal.port"      # the launcher reads this to find us


def running_portal() -> str | None:
    """URL of a portal that is already up, so a second launch doesn't start a second scanner."""
    try:
        url = f"http://127.0.0.1:{int(PORT_FILE.read_text().strip())}/"
        with urllib.request.urlopen(url + "api/ping", timeout=1) as r:
            return url if json.load(r).get("app") == "solpaper-portal" else None
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="solpaper local portal (simulator only).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--autostart", action="store_true", help="start the scanner and study")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--start", default="", help="comma-separated: scanner,study")
    ap.add_argument("--wait-port", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--config")
    a = ap.parse_args(argv)

    already = None if a.wait_port else running_portal()
    if already:
        print(f"solpaper portal is already running: {already}", flush=True)
        if not a.no_browser:
            webbrowser.open(already)
        return 0

    portal = Portal(load_config(a.config), ROOT)
    httpd = None
    if a.wait_port:      # the old portal is stopping its processes; take over its port
        for _ in range(180):
            try:
                httpd = serve(portal, a.port)
                break
            except OSError:
                time.sleep(0.5)
    if httpd is None:
        try:
            httpd = serve(portal, a.port, tries=20)
        except OSError:
            raise SystemExit(f"no free port between {a.port} and {a.port + 19}")
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    PORT_FILE.parent.mkdir(exist_ok=True)
    PORT_FILE.write_text(str(port))
    print(f"solpaper portal: {url}  (simulator only - paper money)", flush=True)

    wanted = list(portal.procs.commands) if a.autostart else \
        [n for n in a.start.split(",") if n in portal.procs.commands]
    for name in wanted:
        portal.procs.start(name)
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    handing_over = []

    def restart():
        """Start a fresh portal on the same port (it waits for this one to let go), with
        the same processes running, then shut this one down."""
        running = [n for n in portal.procs.commands if portal.procs.running(n)]
        py = Path(sys.executable)
        if os.name == "nt" and (py.parent / "pythonw.exe").exists():
            py = py.parent / "pythonw.exe"           # no console window for the new portal
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        with open(ROOT / "logs" / "portal.log", "a", encoding="utf-8") as log:
            subprocess.Popen([str(py), "-m", "solpaper.portal", "--no-browser", "--wait-port",
                              "--port", str(port), "--start", ",".join(running)], cwd=ROOT,
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != "nt")
        handing_over.append(True)
        shutdown()

    portal.on_quit = shutdown
    portal.on_restart = restart
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        print("stopping scanner and study...", flush=True)
        portal.procs.stop_all()
        httpd.server_close()
        if not handing_over:                     # the new portal owns the port file now
            PORT_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

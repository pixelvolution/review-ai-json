"""One-click launcher for the solpaper portal (becomes solpaper.exe via build_exe.bat).

Standard library only, so PyInstaller bundles almost nothing. It does not
contain solpaper itself: it starts the portal with the repo's own .venv
Python, so a `git pull` updates the app without rebuilding the .exe.

  * Portal already running -> just open it in the browser.
  * Otherwise start `python -m solpaper.portal --autostart` with no console
    window, wait for it to answer, then open the browser.

Simulator only: nothing here holds keys, signs or sends anything.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8765


def repo_dir() -> Path:
    # The .exe sits in the repo folder; the .py does too.
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def venv_python(root: Path) -> Path | None:
    for rel in (r".venv\Scripts\pythonw.exe", r".venv\Scripts\python.exe",
                ".venv/bin/python"):
        p = root / rel
        if p.exists():
            return p
    return None


def portal_url(root: Path) -> str:
    """The portal writes its port to logs/portal.port (8765 unless that was taken)."""
    try:
        port = int((root / "logs" / "portal.port").read_text().strip())
    except (OSError, ValueError):
        port = PORT
    return f"http://127.0.0.1:{port}/"


def portal_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=1) as r:
            return json.load(r).get("app") == "solpaper-portal"
    except (OSError, ValueError):
        return False


def message(text: str):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, "solpaper", 0x10)
    else:
        print(text, file=sys.stderr)


def main() -> int:
    root = repo_dir()
    if portal_up(portal_url(root)):
        webbrowser.open(portal_url(root))
        return 0
    (root / "logs" / "portal.port").unlink(missing_ok=True)    # stale: left by a crash
    py = venv_python(root)
    if py is None or not (root / "solpaper").is_dir():
        message(f"Can't find solpaper's Python in\n{root}\\.venv\n\n"
                "Put solpaper.exe in the repo folder and run 'Setup solpaper.bat' once.")
        return 1
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
    (root / "logs").mkdir(exist_ok=True)
    with open(root / "logs" / "portal.log", "a", encoding="utf-8") as log:
        subprocess.Popen([str(py), "-m", "solpaper.portal", "--autostart", "--no-browser",
                          "--port", str(PORT)], cwd=root, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags)
    for _ in range(60):
        url = portal_url(root)
        if (root / "logs" / "portal.port").exists() and portal_up(url):
            webbrowser.open(url)
            return 0
        time.sleep(0.5)
    message("The portal didn't start within 30 seconds.\n\nSee logs\\portal.log in the repo folder.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

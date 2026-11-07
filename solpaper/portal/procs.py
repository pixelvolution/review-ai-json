"""Start and stop the scanner and the study as child processes, with log files."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

COMMANDS = {
    "scanner": ["-m", "solpaper.scanner"],
    "study": ["-m", "solpaper.study", "run"],
}
WINDOWS = os.name == "nt"


class ProcessManager:
    def __init__(self, root: Path, log_dir: Path, python: str | None = None,
                 commands: dict | None = None):
        self.root, self.log_dir = root, log_dir
        self.python = python or sys.executable
        self.commands = commands or COMMANDS
        self.procs: dict[str, subprocess.Popen] = {}
        self.started_at: dict[str, float] = {}
        log_dir.mkdir(parents=True, exist_ok=True)

    def log_path(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def running(self, name: str) -> bool:
        p = self.procs.get(name)
        return p is not None and p.poll() is None

    def status(self) -> dict:
        out = {}
        for name in self.commands:
            p = self.procs.get(name)
            out[name] = {"running": self.running(name),
                         "pid": p.pid if p else None,
                         "exit_code": None if (p is None or p.poll() is None) else p.returncode,
                         "started_at": self.started_at.get(name)}
        return out

    def start(self, name: str) -> dict:
        if name not in self.commands:
            raise KeyError(name)
        if self.running(name):
            return self.status()[name]
        log = open(self.log_path(name), "a", encoding="utf-8")
        log.write(f"\n===== started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        log.flush()
        kwargs = {}
        if WINDOWS:   # own process group (so Ctrl+Break reaches it) and no console window
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP |
                                       getattr(subprocess, "CREATE_NO_WINDOW", 0))
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        self.procs[name] = subprocess.Popen([self.python, *self.commands[name]], cwd=self.root,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, env=env, **kwargs)
        self.started_at[name] = time.time()
        return self.status()[name]

    def stop(self, name: str, timeout: float = 15) -> dict:
        p = self.procs.get(name)
        if p is None or p.poll() is not None:
            return self.status().get(name, {"running": False})
        try:   # ask nicely first: the scanner and study finish their current step and exit
            p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
            p.wait(timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        return self.status()[name]

    def stop_all(self):
        for name in list(self.procs):
            self.stop(name)

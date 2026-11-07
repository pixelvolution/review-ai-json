"""Launch Outcome Study process.

    python -m solpaper.study run              # run until stopped (needs the scanner running)
    python -m solpaper.study run --minutes 30
    python -m solpaper.study stats
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from ..config import load_config
from ..db import connect, event
from . import schema
from .runner import StudyRunner
from .stats import report

log = logging.getLogger("solpaper.study")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Launch Outcome Study (passive, read-only).")
    ap.add_argument("cmd", choices=["run", "stats"])
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "stats":
        conn = connect(cfg["db_path"])
        schema.create(conn)
        print(report(conn))
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    runner = StudyRunner(cfg)
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    event(runner.conn, "study_start", f"study run {runner.run_id} started")
    end = None if a.minutes is None else time.monotonic() + a.minutes * 60
    while not stop["now"] and (end is None or time.monotonic() < end):
        try:
            r = runner.tick()
            if r["added"] or r["features"] or r["outcomes"]:
                log.info("cohort +%d   features captured %d   outcomes labelled %d   gaps %d",
                         r["added"], r["features"], r["outcomes"], r["gaps"])
        except Exception as e:  # noqa: BLE001 - keep the study alive; log and continue
            log.exception("study tick failed")
            event(runner.conn, "study_error", f"{type(e).__name__}: {e}")
        time.sleep(cfg["study"]["tick_s"])
    event(runner.conn, "study_stop", f"study run {runner.run_id} stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

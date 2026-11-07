"""Study scheduler: sync the cohort, then work through due tasks in the queue.

The queue lives in SQLite (``study_queue``), so a restarted process picks up
where the last one stopped. A task that runs after its window's cutoff still
gets the observation-based features, computed at the cutoff, but no live
reads; that is recorded as a gap, never filled in.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ..client import Http
from ..db import connect, event, now_iso
from . import curve as curve_mod
from . import schema
from .cohort import mark_graduate, parse_ts, start_run, sync
from .features import compute

FEATURE_COLUMNS = (
    "n_obs", "liquidity_usd", "price_usd", "price_change", "vol_m5", "vol_h1", "buys_h1",
    "sells_h1", "buy_sell_ratio", "graduated", "minutes_to_graduate", "curve_read_at",
    "curve_complete", "curve_price_sol", "curve_virtual_sol", "curve_virtual_tokens",
    "curve_real_tokens", "curve_real_sol", "curve_consistent")


class StudyRunner:
    def __init__(self, cfg: dict, conn=None, http: Http | None = None, wall=time.time,
                 seed: str | None = None):
        self.cfg = cfg
        self.conn = conn or connect(cfg["db_path"])
        schema.create(self.conn)
        self.http = http or Http(self.conn, cfg)
        self.wall = wall
        self.run_id, self.seed = start_run(self.conn, cfg, seed)

    def tick(self) -> dict:
        added = sync(self.conn, self.cfg, self.run_id, self.seed)
        done, gaps = self.process_due()
        return {"added": added, "features": done, "gaps": gaps}

    def _gap(self, pair, window, reason):
        self.conn.execute("INSERT INTO study_gaps (pair_address, task, window_min, ts, reason) "
                          "VALUES (?, 'features', ?, ?, ?)", (pair, window, now_iso(), reason))

    def process_due(self, limit: int = 500) -> tuple[int, int]:
        now = self.wall()
        tasks = self.conn.execute("""
            SELECT q.id, q.window_min, c.* FROM study_queue q
            JOIN launch_cohort c USING (pair_address)
            WHERE q.status = 'pending' AND q.task = 'features' AND q.due_at <= ?
            ORDER BY q.due_at LIMIT ?""", (now, limit)).fetchall()
        if not tasks:
            return 0, 0
        cutoff_of = {t["id"]: parse_ts(t["first_seen_at"]).timestamp() + t["window_min"] * 60
                     for t in tasks}
        live = [t for t in tasks if now <= cutoff_of[t["id"]]]
        addrs = sorted({t["pair_address"] for t in live if t["source"] == "pumpfun_curve"})
        curves, read_at = {}, None
        if addrs:
            read_at = datetime.fromtimestamp(self.wall(), timezone.utc)
            curves = curve_mod.read(self.http, addrs, self.cfg["study"]["pump_program"])
        done = gaps = 0
        for t in tasks:
            pair, w = t["pair_address"], t["window_min"]
            if now <= cutoff_of[t["id"]]:
                cutoff = datetime.fromtimestamp(now, timezone.utc)
                cv, why = None, None
                if t["source"] == "pumpfun_curve":
                    if curves is None:
                        why = "rpc unavailable at capture time"
                        self._gap(pair, w, why)
                        gaps += 1
                    else:
                        cv = curves.get(pair)
            else:
                cutoff = datetime.fromtimestamp(cutoff_of[t["id"]], timezone.utc)
                cv = None
                why = "late: task ran after the window cutoff, live read impossible"
                self._gap(pair, w, why)
                gaps += 1
            f = compute(self.conn, self.cfg, t, w, cutoff, cv,
                        read_at if cv is not None else None, why)
            cols = [c for c in FEATURE_COLUMNS if c in f]
            self.conn.execute(
                f"INSERT OR IGNORE INTO launch_features (pair_address, window_min, data_cutoff, "
                f"captured_at, missing{''.join(', ' + c for c in cols)}) VALUES (?, ?, ?, ?, ?"
                f"{', ?' * len(cols)})",
                (pair, w, cutoff.isoformat(timespec="milliseconds"), now_iso(),
                 json.dumps(f["missing"]), *[f[c] for c in cols]))
            self.conn.execute("UPDATE study_queue SET status = 'done', attempts = attempts + 1, "
                              "updated_at = ? WHERE id = ?", (now_iso(), t["id"]))
            if f.get("graduated"):
                mark_graduate(self.conn, pair)
            done += 1
        if gaps:
            event(self.conn, "study_gap", f"{gaps} study feature task(s) recorded as gaps")
        return done, gaps

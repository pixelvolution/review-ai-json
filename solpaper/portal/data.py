"""Read-only views of the database for the portal. Missing tables = empty sections."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _fmt_t(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


def summary(db_path: str) -> dict:
    if not Path(db_path).exists():
        return {"db": False}
    conn = open_db(db_path)
    try:
        have = _tables(conn)
        one = lambda sql, *a: conn.execute(sql, a).fetchone()
        out: dict = {"db": True}
        if "pairs" in have:
            out["capture"] = {
                "pairs": one("SELECT COUNT(*) FROM pairs")[0],
                "observations": one("SELECT COUNT(*) FROM pairs_seen")[0],
                "last_observation": one("SELECT MAX(observed_at) FROM pairs_seen")[0],
            }
        if "data_gaps" in have:
            out["gaps"] = [dict(r) for r in conn.execute(
                "SELECT provider, started_at, ended_at, attempts, last_status FROM data_gaps "
                "ORDER BY id DESC LIMIT 5")]
            out["gaps_open"] = one("SELECT COUNT(*) FROM data_gaps WHERE ended_at IS NULL")[0]
        if "filter_state" in have:
            total = one("SELECT COUNT(*) FROM filter_state")[0]
            out["filters"] = {
                "evaluated": total,
                "passed": one("SELECT COUNT(*) FROM filter_state WHERE outcome = 'pass'")[0],
                "reasons": [dict(r) for r in conn.execute(
                    "SELECT reason, COUNT(*) n FROM filter_state WHERE outcome = 'skip' "
                    "GROUP BY reason ORDER BY n DESC LIMIT 8")]}
        if "wallet_runs" in have:
            w = one("SELECT * FROM wallet_runs ORDER BY run_id DESC LIMIT 1")
            if w:
                bal, vault = w["balance"] / 1e6, w["vault"] / 1e6
                open_val = 0.0
                if "wallet_positions" in have:
                    for p in conn.execute("SELECT qty, mark_price FROM wallet_positions WHERE "
                                          "run_id = ? AND status = 'open'", (w["run_id"],)):
                        open_val += float(p["qty"]) * float(p["mark_price"])
                peak = w["peak_equity"] / 1e6
                equity = bal + open_val
                out["wallet"] = {
                    "run": w["run_id"], "status": w["status"], "start": w["start_balance"] / 1e6,
                    "balance": bal, "vault": vault, "open_value": open_val, "equity": equity,
                    "peak": peak, "drawdown": max(0.0, (peak - equity) / peak) if peak else 0.0,
                    "worst_drawdown": float(w["max_drawdown"] or 0),
                    "cooked_reason": w["cooked_reason"]}
        if "trades" in have:
            # This wallet run only: a new run starts the count from zero.
            run = out.get("wallet", {}).get("run")
            t = one("""SELECT COUNT(*) n, SUM(status IN ('closed','failed_sell')) done,
                       SUM(CASE WHEN status IN ('closed','failed_sell') THEN pnl END) pnl,
                       SUM(CASE WHEN status IN ('closed','failed_sell') AND pnl > 0 THEN 1 ELSE 0 END) wins,
                       SUM(fees) fees, SUM(tips) tips FROM trades
                       WHERE ? IS NULL OR wallet_run_id = ?""", run, run)
            out["trades"] = {"run": run, "total": t["n"], "closed": t["done"] or 0, "pnl": t["pnl"] or 0.0,
                             "wins": t["wins"] or 0, "fees": t["fees"] or 0.0, "tips": t["tips"] or 0.0}
        if "launch_cohort" in have:
            out["study"] = {
                "launches": one("SELECT COUNT(*) FROM launch_cohort WHERE status = 'launch'")[0],
                "sampled": one("SELECT COUNT(*) FROM launch_cohort WHERE sampled = 1")[0],
                "features": one("SELECT COUNT(*) FROM launch_features")[0]
                if "launch_features" in have else 0,
                "outcomes": [dict(r) for r in conn.execute(
                    "SELECT label, COUNT(*) n FROM launch_outcomes GROUP BY label ORDER BY n DESC")]
                if "launch_outcomes" in have else [],
                "gaps": one("SELECT COUNT(*) FROM study_gaps")[0] if "study_gaps" in have else 0}
        return out
    finally:
        conn.close()


def positions(db_path: str) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = open_db(db_path)
    try:
        if "trades" not in _tables(conn):
            return []
        rows = conn.execute("""
            SELECT t.id, t.name, t.status, t.notional, t.entry_mid, t.entry_price, t.entry_fill_t,
                   t.exit_reason, p.mark_price, p.qty
            FROM trades t LEFT JOIN wallet_positions p ON p.position_id = t.position_id
            WHERE t.status IN ('pending_entry', 'open', 'pending_exit') ORDER BY t.id DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if r["mark_price"] and r["entry_mid"]:
                d["move"] = float(r["mark_price"]) / r["entry_mid"] - 1
            d["entry_fill"] = _fmt_t(r["entry_fill_t"])
            out.append(d)
        return out
    finally:
        conn.close()


def trades(db_path: str, limit: int = 20) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = open_db(db_path)
    try:
        if "trades" not in _tables(conn):
            return []
        return [{**dict(r), "exit_fill": _fmt_t(r["exit_fill_t"])} for r in conn.execute(
            "SELECT wallet_run_id run, name, status, exit_reason, notional, pnl, exit_fill_t, "
            "entry_impact, exit_impact "
            "FROM trades WHERE status IN ('closed', 'failed_sell', 'entry_failed') "
            "ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def signals(db_path: str, limit: int = 25) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = open_db(db_path)
    try:
        have = _tables(conn)
        out = []
        if "signals" in have:
            for r in conn.execute("SELECT s.t, s.kind, s.reason, p.name FROM signals s "
                                  "LEFT JOIN pairs p USING (pair_address) ORDER BY s.id DESC LIMIT ?",
                                  (limit,)):
                out.append({"time": _fmt_t(r["t"]), "kind": r["kind"], "reason": r["reason"],
                            "name": r["name"]})
        return out
    finally:
        conn.close()


def equity(db_path: str, limit: int = 500) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = open_db(db_path)
    try:
        if "wallet_snapshots" not in _tables(conn):
            return []
        run = conn.execute("SELECT MAX(run_id) FROM wallet_runs").fetchone()[0]
        rows = conn.execute("SELECT ts, equity, vault FROM wallet_snapshots WHERE run_id = ? "
                            "ORDER BY snapshot_id DESC LIMIT ?", (run, limit)).fetchall()
        return [{"ts": r["ts"], "equity": r["equity"] / 1e6, "vault": r["vault"] / 1e6}
                for r in reversed(rows)]
    finally:
        conn.close()


def tail(path: Path, lines: int = 60) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        text = fh.read().decode("utf-8", "replace")
    return text.splitlines()[-lines:]

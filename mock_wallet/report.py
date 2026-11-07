"""Wallet status, run history and the morning report.

    python report.py            # print the morning report (markdown)
    python report.py --json     # same data as JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from wallet import MICRO, D, MockWallet, connect, from_micros, q_down


def wallet_status(wallet: MockWallet) -> dict:
    """Headline numbers for the current run."""
    run = wallet._run()
    open_value = wallet.open_position_value()
    return {
        "run_id": wallet.run_id,
        "status": run["status"],
        "started_at": run["started_at"],
        "cooked_at": run["cooked_at"],
        "cooked_reason": run["cooked_reason"],
        "start_balance": wallet.start_balance,
        "balance": wallet.balance,
        "vault": wallet.vault,
        "open_position_value": open_value,
        "open_positions": len(wallet.open_positions()),
        "equity": wallet.balance + open_value,
        "peak_equity": wallet.peak_equity,
        "drawdown": wallet.drawdown(),
        "total_value": wallet.balance + open_value + wallet.vault,
    }


def runs_history(db_path=None) -> list[dict]:
    """One row per life, oldest first, for comparing runs."""
    conn = connect(db_path)
    try:
        rows = conn.execute("""
            SELECT r.*,
              (SELECT MAX(CAST(drawdown AS REAL)) FROM snapshots s WHERE s.run_id = r.run_id)
                AS snapshot_max_drawdown,
              (SELECT COUNT(*) FROM positions p WHERE p.run_id = r.run_id) AS trades,
              (SELECT COUNT(*) FROM positions p WHERE p.run_id = r.run_id AND p.pnl > 0) AS wins,
              (SELECT COUNT(*) FROM positions p WHERE p.run_id = r.run_id AND p.status = 'failed')
                AS failed_sells,
              (SELECT COALESCE(SUM(-amount), 0) FROM ledger l
                 WHERE l.run_id = r.run_id AND l.kind IN ('fee', 'tip')) AS costs,
              (SELECT COALESCE(SUM(entry_slippage + COALESCE(exit_slippage, 0)), 0)
                 FROM positions p WHERE p.run_id = r.run_id) AS slippage
            FROM runs r ORDER BY r.run_id""").fetchall()
        out = []
        for r in rows:
            start = from_micros(r["start_balance"])
            open_value = q_down(sum((D(p["qty"]) * D(p["mark_price"]) for p in conn.execute(
                "SELECT qty, mark_price FROM positions WHERE run_id = ? AND status = 'open'",
                (r["run_id"],))), Decimal(0)))
            final = from_micros(r["balance"]) + open_value + from_micros(r["vault"])
            out.append({
                "run_id": r["run_id"],
                "status": r["status"],
                "started_at": r["started_at"],
                "cooked_at": r["cooked_at"],
                "cooked_reason": r["cooked_reason"],
                "start_balance": start,
                "balance": from_micros(r["balance"]),
                "vault": from_micros(r["vault"]),
                "peak_equity": from_micros(r["peak_equity"]),
                "max_drawdown": max(D(r["max_drawdown"]),
                                    D(r["snapshot_max_drawdown"] or 0)).quantize(MICRO),
                "trades": r["trades"],
                "wins": r["wins"],
                "failed_sells": r["failed_sells"],
                "costs": from_micros(r["costs"]),
                "slippage": from_micros(r["slippage"]),
                "return": (final - start) / start,
            })
        return out
    finally:
        conn.close()


def recent_events(db_path=None, limit: int = 20, run_id: int | None = None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT run_id, ts, kind, message FROM events"
        args: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id = ?"
            args = (run_id,)
        sql += " ORDER BY event_id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, args + (limit,)).fetchall()]
    finally:
        conn.close()


def _usd(x: Decimal) -> str:
    return f"${x:,.2f}"


def _pct(x: Decimal) -> str:
    return f"{x:.2%}"


def morning_report(db_path=None) -> str:
    wallet = MockWallet(db_path, create=False)
    try:
        s = wallet_status(wallet)
    finally:
        wallet.close()
    status = "ALIVE" if s["status"] == "alive" else "COOKED"
    lines = [
        f"# Mock Wallet: Run #{s['run_id']} ({status})",
        "",
        "| | |",
        "|---|---|",
        f"| Run number | #{s['run_id']} |",
        f"| Run status | {status} |",
        f"| Start balance | {_usd(s['start_balance'])} |",
        f"| Current balance | {_usd(s['balance'])} |",
        f"| Open position value | {_usd(s['open_position_value'])} "
        f"({s['open_positions']} open) |",
        f"| Equity | {_usd(s['equity'])} |",
        f"| Vault | {_usd(s['vault'])} |",
        f"| Peak equity | {_usd(s['peak_equity'])} |",
        f"| Current drawdown | {_pct(s['drawdown'])} |",
    ]
    if s["status"] == "cooked":
        lines += ["", f"**Cooked at {s['cooked_at']}:** {s['cooked_reason']}",
                  "", "Trading is halted. Run `python reset.py` to start a new run."]
    history = runs_history(db_path)
    if len(history) > 1:
        lines += ["", "## All runs", "",
                  "| Run | Status | Start | Balance | Vault | Peak | Max DD | Trades | Return |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for h in history:
            lines.append(
                f"| #{h['run_id']} | {h['status']} | {_usd(h['start_balance'])} | "
                f"{_usd(h['balance'])} | {_usd(h['vault'])} | {_usd(h['peak_equity'])} | "
                f"{_pct(h['max_drawdown'])} | {h['trades']} | {_pct(h['return'])} |")
    events = recent_events(db_path, limit=10, run_id=s["run_id"])
    if events:
        lines += ["", "## Recent events", ""]
        lines += [f"- `{e['ts']}` **{e['kind']}**: {e['message']}" for e in events]
    return "\n".join(lines) + "\n"


def _jsonable(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Mock wallet morning report")
    p.add_argument("--db", help="path to the wallet database")
    p.add_argument("--json", action="store_true", help="print status + history as JSON")
    args = p.parse_args(argv)
    try:
        if args.json:
            w = MockWallet(args.db, create=False)
            try:
                data = {"status": wallet_status(w), "runs": runs_history(args.db)}
            finally:
                w.close()
            print(json.dumps(data, default=_jsonable, indent=2))
        else:
            print(morning_report(args.db), end="")
    except Exception as exc:  # noqa: BLE001 - CLI surface
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

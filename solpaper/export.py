"""Export captured data and findings to CSV files that open in Excel.

    python -m solpaper.export                  # everything, into .\\exports
    python -m solpaper.export --out shared     # into another folder
    python -m solpaper.export --only study     # study | filters | scanner

Files are UTF-8 with a byte-order mark, so Excel shows emoji and non-English
coin names correctly (Windows' default encoding can't store them). A table
that doesn't exist yet (e.g. the study has never run) is skipped with a note.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .config import dex_label, load_config
from .db import connect

# group -> [(file name, required tables, query)]
QUERIES = {
    "scanner": [
        ("readings.csv", ["pairs", "pairs_seen"],
         "SELECT p.name, p.dex, s.* FROM pairs_seen s JOIN pairs p USING (pair_address) "
         "ORDER BY s.id"),
        ("pairs.csv", ["pairs"], "SELECT * FROM pairs ORDER BY first_seen_at"),
    ],
    "filters": [
        ("filter_decisions.csv", ["filter_state", "pairs"],
         "SELECT p.name, p.dex, p.pair_created_at, f.outcome, f.reason, f.updated_at, "
         "(SELECT detail FROM skips k WHERE k.pair_address = f.pair_address "
         " ORDER BY k.id DESC LIMIT 1) AS last_skip_detail, f.pair_address, p.base_mint "
         "FROM filter_state f JOIN pairs p USING (pair_address) ORDER BY f.outcome, f.reason"),
        ("filter_history.csv", ["skips", "filter_passes", "pairs"],
         "SELECT h.ts, p.name, p.dex, h.outcome, h.stage, h.reason, h.detail, h.metrics, "
         "h.pair_address FROM ("
         " SELECT ts, pair_address, 'skip' AS outcome, stage, reason, detail, metrics FROM skips"
         " UNION ALL SELECT ts, pair_address, 'pass', NULL, NULL, NULL, metrics FROM filter_passes"
         ") h JOIN pairs p USING (pair_address) ORDER BY h.ts"),
        ("sell_quotes.csv", ["sell_quotes", "pairs"],
         "SELECT q.ts, p.name, q.ok, q.reason, q.usd_in, q.measured_impact, q.price_impact AS "
         "jupiter_reported_impact, q.impact_disagrees, q.effective_vs_pool, q.routes, "
         "q.pair_address, q.mint FROM sell_quotes q JOIN pairs p USING (pair_address) ORDER BY q.id"),
        ("token_checks.csv", ["token_checks"], "SELECT * FROM token_checks ORDER BY checked_at"),
    ],
    "trading": [
        ("trades.csv", ["trades"],
         "SELECT id, name, status, entry_reason, notional, entry_signal_t, entry_fill_t, "
         "entry_delay_s, entry_mid, entry_price, entry_impact, entry_liquidity, honeypot, "
         "exit_reason, exit_signal_t, exit_fill_t, exit_delay_s, exit_mid, exit_price, exit_impact, "
         "fees, tips, proceeds, pnl, note, pair_address, wallet_run_id FROM trades ORDER BY id"),
        ("signals.csv", ["signals"], "SELECT * FROM signals ORDER BY id"),
        ("wallet_runs.csv", ["wallet_runs"],
         "SELECT run_id, started_at, status, start_balance / 1e6 AS start_balance, "
         "balance / 1e6 AS balance, vault / 1e6 AS vault, peak_equity / 1e6 AS peak_equity, "
         "max_drawdown, cooked_at, cooked_reason FROM wallet_runs ORDER BY run_id"),
        ("wallet_snapshots.csv", ["wallet_snapshots"],
         "SELECT run_id, ts, trigger, balance / 1e6 AS balance, vault / 1e6 AS vault, "
         "open_position_value / 1e6 AS open_position_value, equity / 1e6 AS equity, "
         "peak_equity / 1e6 AS peak_equity, drawdown FROM wallet_snapshots ORDER BY snapshot_id"),
        ("position_marks.csv", ["position_marks", "pairs"],
         "SELECT p.name, m.* FROM position_marks m JOIN pairs p USING (pair_address) ORDER BY m.id"),
        ("pool_reads.csv", ["pool_reads", "pairs"],
         "SELECT p.name, p.dex, r.* FROM pool_reads r JOIN pairs p USING (pair_address)"),
    ],
    "study": [
        ("study_cohort.csv", ["launch_cohort", "pairs"],
         "SELECT c.first_seen_at, p.name, c.source, c.dex, c.status, c.pair_created_at, "
         "c.sampled, c.sampled_reason, c.sample_rate, c.prior_curve_pair, c.run_id, "
         "c.pair_address, c.mint FROM launch_cohort c JOIN pairs p USING (pair_address) "
         "ORDER BY c.first_seen_at"),
        ("study_features.csv", ["launch_features", "launch_cohort", "pairs"],
         "SELECT p.name, c.source, c.sampled, f.* FROM launch_features f "
         "JOIN launch_cohort c USING (pair_address) JOIN pairs p USING (pair_address) "
         "ORDER BY f.pair_address, f.window_min"),
        ("study_gaps.csv", ["study_gaps"], "SELECT * FROM study_gaps ORDER BY id"),
        ("study_runs.csv", ["study_runs"], "SELECT * FROM study_runs ORDER BY run_id"),
    ],
}


def export(db_path: str, out_dir: Path, cfg: dict | None = None,
           only: str | None = None) -> dict[str, int | None]:
    """Write CSVs. Returns rows per file; None = skipped (table not created yet)."""
    cfg = cfg or load_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts: dict[str, int | None] = {}
    for group, items in QUERIES.items():
        if only and group != only:
            continue
        for name, tables, sql in items:
            if not set(tables) <= have:
                counts[name] = None
                continue
            cur = conn.execute(sql)
            with open(out_dir / name, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                cols = [d[0] for d in cur.description]
                w.writerow(cols)
                dex_i = cols.index("dex") if "dex" in cols else None
                n = 0
                for row in cur:
                    row = list(row)
                    if dex_i is not None:
                        row[dex_i] = dex_label(cfg, row[dex_i])
                    w.writerow(row)
                    n += 1
            counts[name] = n
    write_readme(conn, out_dir, counts, have)
    return counts


FILE_NOTES = {
    "readings.csv": "every price/liquidity/volume reading the scanner recorded (one row per reading)",
    "pairs.csv": "one row per coin pair the scanner found, with where it was first seen",
    "filter_decisions.csv": "each pair's current filter decision (pass, or the skip reason) and why",
    "filter_history.csv": "every change in a pair's filter decision over time",
    "sell_quotes.csv": "Jupiter sell checks: measured price impact of a $10 sale vs a $0.50 sale",
    "token_checks.csv": "on-chain token checks: mint/freeze authority and Token-2022 extensions",
    "study_cohort.csv": "Launch Outcome Study cohort: every pair, its source, launch status, sampling",
    "study_features.csv": "early-feature snapshots at 5/15/60 min after first seen (frozen, no look-ahead); "
                          "the 'missing' column says why any field is empty",
    "study_gaps.csv": "study tasks that couldn't be captured on time (never filled in with guesses)",
    "study_runs.csv": "study process runs, their sampling seed and config",
    "trades.csv": "paper trades: entry/exit signal and fill times, delays, impact, fees, tips, P&L",
    "signals.csv": "every entry and exit decision with its reason",
    "wallet_runs.csv": "mock-wallet runs (lives): balance, vault, peak, drawdown, cooked cause",
    "wallet_snapshots.csv": "wallet state after every open and close",
    "position_marks.csv": "on-chain price reads (every ~5 s) of pumpswap pools the paper trader "
                          "held: reserves, price, liquidity",
    "pool_reads.csv": "which held pools are read on-chain, and why not for the others",
}


def write_readme(conn, out_dir: Path, counts: dict, have: set) -> None:
    """Every export states its sample size, time window and caveats up front."""
    one = lambda sql: conn.execute(sql).fetchone()
    lines = ["solpaper export: PAPER-TRADING RESEARCH DATA (simulator only, no real money)", ""]
    if "pairs_seen" in have:
        w = one("SELECT MIN(observed_at), MAX(observed_at), COUNT(*) FROM pairs_seen")
        lines.append(f"Scanner window: {w[0]} -> {w[1]}   readings: {w[2]}")
    if "launch_cohort" in have:
        c = one("SELECT COUNT(*), SUM(status = 'launch'), MIN(first_seen_at), MAX(first_seen_at) "
                "FROM launch_cohort")
        lines.append(f"Study sample: {c[1] or 0} launches ({c[0]} pairs seen), "
                     f"first seen {c[2]} -> {c[3]}")
    lines += [
        "",
        "Read before sharing or drawing conclusions:",
        "- This is a short observation window. Patterns here are hypotheses, not a tradable edge.",
        "- Coverage is unknown: discovery sees ~20 new pools a minute and more launch than that.",
        "- Empty cells mean unknown (the data wasn't available), never zero.",
        "- Study outcomes and labels (did a launch survive or flop) are not collected yet.",
        "",
        "Files:",
    ]
    for name, n in counts.items():
        state = "not created yet (skipped)" if n is None else f"{n} rows"
        lines.append(f"- {name} ({state}): {FILE_NOTES.get(name, '')}")
    (out_dir / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export solpaper data and findings to CSV.")
    ap.add_argument("--out", default="exports", help="folder to write into (default: exports)")
    ap.add_argument("--only", choices=list(QUERIES), help="export one group only")
    a = ap.parse_args(argv)
    out = Path(a.out)
    for name, n in export(load_config()["db_path"], out, only=a.only).items():
        print(f"{out / name}: " + ("skipped (not created yet)" if n is None else f"{n} rows"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Start a new run: ``python reset.py``.

This is the ONLY way to start run N+1. It never edits or erases an earlier
run: cooked runs stay in the database as they died, for comparing lives.

    python reset.py                          # new run, default $100
    python reset.py --start-balance 250      # new run with a different stake
    python reset.py --force                  # cook a still-alive run first
                                             # (liquidated at last marks)
"""

from __future__ import annotations

import argparse
import logging
import sys

from wallet import MockWallet, WalletConfig, WalletError, connect, start_new_run


def reset(db_path=None, config: WalletConfig | None = None, force: bool = False) -> int:
    """Create run N+1 and return its run_id."""
    conn = connect(db_path)
    try:
        has_runs = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is not None
        alive = conn.execute("SELECT run_id FROM runs WHERE status = 'alive'").fetchone()
    finally:
        conn.close()

    if alive is not None:
        if not force:
            raise WalletError(
                f"run {alive['run_id']} is still alive. Reset only starts a new run after "
                f"a run is cooked. Use --force to cook run {alive['run_id']} now "
                f"(open positions are liquidated at their last marks).")
        w = MockWallet(db_path, create=False)
        try:
            w.cook("manual reset (run was abandoned while alive)")
        finally:
            w.close()

    if not has_runs:
        w = MockWallet(db_path, config=config or WalletConfig.from_env())
        try:
            return w.run_id
        finally:
            w.close()
    return start_new_run(db_path, config or WalletConfig.from_env())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Start a new mock wallet run (run N+1).")
    p.add_argument("--db", help="path to the wallet database")
    p.add_argument("--start-balance")
    p.add_argument("--min-trade-size")
    p.add_argument("--max-drawdown", help="kill switch, e.g. 0.25")
    p.add_argument("--vault-target-multiple", help="cup rule target, e.g. 2")
    p.add_argument("--position-fraction", help="fraction of equity per trade")
    p.add_argument("--fee-rate")
    p.add_argument("--slippage-rate")
    p.add_argument("--tip")
    p.add_argument("--force", action="store_true",
                   help="cook a run that is still alive, then start a new one")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = WalletConfig.from_env(
            start_balance=args.start_balance, min_trade_size=args.min_trade_size,
            max_drawdown=args.max_drawdown, vault_target_multiple=args.vault_target_multiple,
            position_fraction=args.position_fraction, fee_rate=args.fee_rate,
            slippage_rate=args.slippage_rate, tip=args.tip)
        run_id = reset(args.db, cfg, force=args.force)
    except (WalletError, ValueError) as exc:
        print(f"reset refused: {exc}", file=sys.stderr)
        return 1
    print(f"run #{run_id} is alive with ${cfg.start_balance}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Start a new mock-wallet run: ``python -m solpaper.reset`` (or ``python reset.py``).

The only way to start run N+1. It never edits or erases an earlier run.

    python -m solpaper.reset            # new run with the wallet settings in config.yaml
    python -m solpaper.reset --force    # cook a still-alive run first (liquidated at last marks)
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .wallet import MockWallet, WalletConfig, WalletError, connect, start_new_run


def wallet_config(cfg: dict) -> WalletConfig:
    return WalletConfig(**cfg["wallet"])


def reset(db_path: str, wcfg: WalletConfig, force: bool = False) -> int:
    conn = connect(db_path)
    try:
        has_runs = conn.execute("SELECT 1 FROM wallet_runs LIMIT 1").fetchone() is not None
        alive = conn.execute("SELECT run_id FROM wallet_runs WHERE status = 'alive'").fetchone()
    finally:
        conn.close()
    if alive is not None:
        if not force:
            raise WalletError(f"run {alive['run_id']} is still alive; reset only starts a new run "
                              f"after one is cooked. Use --force to cook it now.")
        w = MockWallet(db_path, create=False)
        try:
            w.cook("manual reset (run was abandoned while alive)")
        finally:
            w.close()
    if not has_runs:
        w = MockWallet(db_path, config=wcfg)
        try:
            return w.run_id
        finally:
            w.close()
    return start_new_run(db_path, wcfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start a new mock-wallet run (run N+1).")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    try:
        run_id = reset(cfg["db_path"], wallet_config(cfg), a.force)
    except WalletError as e:
        print(f"reset refused: {e}", file=sys.stderr)
        return 1
    print(f"wallet run #{run_id} is alive with ${cfg['wallet']['start_balance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

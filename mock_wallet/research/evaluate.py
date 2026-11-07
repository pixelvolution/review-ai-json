"""Evaluate every strategy x params x cost profile, train vs. held-out test.

    python evaluate.py              # writes results/results.csv and prints a summary

Method:
  * basket: 16 Binance-listed memecoins, 1h candles, 2 years;
  * train = year 1, test = year 2 (never seen while picking params);
  * "edge" mode disables the 25% kill switch so we can measure the strategy
    itself; the kill switch is then applied to the finalists separately;
  * every fill goes through MockWallet with the given cost profile.
"""

from __future__ import annotations

import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from data import MEMES, load
from engine import Panel, backtest
from strategies import STRATEGIES
from wallet import WalletConfig

OUT = Path(__file__).resolve().parent / "results"
SPLIT = pd.Timestamp("2025-11-07", tz="UTC")

PROFILES = {
    "no costs": dict(fee_rate=0, slippage_rate=0, tip=0),
    "CEX 0.1%": dict(fee_rate="0.001", slippage_rate="0.0005", tip=0),
    "lean DEX 0.3%": dict(fee_rate="0.003", slippage_rate="0.003", tip="0.01"),
    "wallet defaults": dict(fee_rate="0.01", slippage_rate="0.01", tip="0.05"),
}

_P = _BTC = None


def _init():
    global _P, _BTC
    _P = Panel.load(MEMES)
    _BTC = load("BTC").set_index("t")["close"].reindex(_P.t)


def periods(panel):
    k = int(np.searchsorted(panel.t, SPLIT))
    return {"train": (0, k), "test": (k, len(panel.t))}


def run_one(task):
    name, params = task
    fn, _ = STRATEGIES[name]
    spec = fn(_P, _BTC, **params)
    if spec.priority is None:  # cash-limited: prefer the most liquid coin
        spec.priority = _P.vol.rolling(24).mean().fillna(0).to_numpy()
    rows = []
    for prof, costs in PROFILES.items():
        cfg = WalletConfig(max_drawdown=1, **costs)
        for per, (a, b) in periods(_P).items():
            r = backtest(_P, spec, cfg, a, b)
            rows.append(dict(strategy=name, params=repr(params), profile=prof, period=per,
                             ret=r.ret, final=r.final_value, cooked=r.cooked, max_dd=r.max_dd,
                             trades=r.trades, win_rate=r.win_rate, avg_trade=r.avg_trade,
                             profit_factor=r.profit_factor, exposure=r.exposure,
                             costs=r.costs))
    return rows


def benchmarks():
    p = Panel.load(MEMES + ["BTC"])
    out = []
    for per, (a, b) in periods(p).items():
        c = p.close.iloc[a:b]
        first = c.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
        last = c.ffill().iloc[-1]
        r = last / first - 1
        out.append(dict(period=per, memes_equal_weight=float(r[MEMES].mean()),
                        memes_median=float(r[MEMES].median()), btc=float(r["BTC"]),
                        memes_up=int((r[MEMES] > 0).sum())))
    return pd.DataFrame(out)


def main():
    OUT.mkdir(exist_ok=True)
    tasks = [(n, prm) for n, (_, g) in STRATEGIES.items() for prm in g]
    print(f"{len(tasks)} configs x {len(PROFILES)} cost profiles x 2 periods", flush=True)
    rows = []
    with Pool(4, initializer=_init) as pool:
        for i, rs in enumerate(pool.imap_unordered(run_one, tasks), 1):
            rows.extend(rs)
            if i % 20 == 0:
                print(f"  {i}/{len(tasks)}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "results.csv", index=False)
    benchmarks().to_csv(OUT / "benchmarks.csv", index=False)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()

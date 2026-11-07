"""Replay a strategy config over a window of stored price history.

Honest fill model, same as the research backtester:
  * the signal at bar i's close fills at bar i + fill_delay's open;
  * slippage, fees and tips are charged by the MockWallet;
  * honeypots: a fixed share of entries can't be sold (total loss);
  * no look-ahead: bars at or after the window end are never loaded, and the
    only bars before the window start are the warmup for indicators.

Paper money only: each replay gets a throwaway in-memory MockWallet.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "research"))

import market  # noqa: E402
from engine import Panel, Result, backtest  # noqa: E402
from strategies import STRATEGIES  # noqa: E402
from wallet import WalletConfig  # noqa: E402


class NoData(Exception):
    pass


def build_panel(bars: dict[str, pd.DataFrame], universe: list[str]) -> Panel:
    frames = {s: bars[s] for s in universe if s in bars}
    if not frames:
        raise NoData("no stored bars for any pair in the universe")
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    get = lambda col: pd.DataFrame({s: f[col].reindex(idx) for s, f in frames.items()})
    return Panel(idx, get("open"), get("high"), get("low"), get("close"), get("volume"))


def run_replay(cfg: dict, window: tuple, settings: dict, wallet_cfg: WalletConfig | None = None,
               market_db=None) -> Result:
    """Run ``cfg`` over ``window`` = (start, end) using stored bars only."""
    start, end = (pd.Timestamp(x, tz="UTC") if pd.Timestamp(x).tzinfo is None
                  else pd.Timestamp(x) for x in window)
    warm = timedelta(hours=settings.get("warmup_hours", 1000))
    pairs = list(dict.fromkeys(cfg["universe"] + [cfg.get("regime_pair", "BTC")]))
    bars = market.load_bars(pairs, start - warm, end, market_db)
    panel = build_panel(bars, cfg["universe"])
    i0 = int(np.searchsorted(panel.t, start))
    if i0 >= len(panel.t):
        raise NoData(f"no stored bars inside {start:%Y-%m-%d} .. {end:%Y-%m-%d}")
    rp = cfg.get("regime_pair", "BTC")
    regime = bars[rp]["close"].reindex(panel.t) if rp in bars else \
        pd.Series(np.nan, index=panel.t)
    fn = STRATEGIES[cfg["strategy"]][0]
    spec = fn(panel, regime, **cfg["params"])
    if spec.priority is None:  # cash-limited: prefer the most liquid pair
        spec.priority = panel.vol.rolling(24).mean().fillna(0).to_numpy()
    return backtest(panel, spec, wallet_cfg or WalletConfig.from_env(), i0, len(panel.t),
                    keep_curve=True, fill_delay=int(settings.get("fill_delay_bars", 1)),
                    honeypot_rate=float(settings.get("honeypot_rate", 0)),
                    honeypot_seed=int(settings.get("honeypot_seed", 0)))

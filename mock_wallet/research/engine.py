"""Portfolio backtester that trades through the real MockWallet.

One wallet, a basket of coins. Every fill goes through ``PaperTrader`` so
fees, slippage, tips, equity-based sizing, the cup rule and the kill switch
apply exactly as they will in paper trading.

Timing (no lookahead):
  * signals are computed on bar i's close and filled at bar i+1's open;
  * stops/targets are checked intrabar on bar i+1 onward; if the low hits
    the stop and the high hits the target in the same bar, the stop wins;
  * a gap through a stop fills at the (worse) open.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from trader import PaperTrader  # noqa: E402
from wallet import MockWallet, RunCooked, WalletConfig  # noqa: E402

from data import load  # noqa: E402


@dataclass
class Panel:
    """Aligned OHLCV for a basket: each field is a DataFrame [time x symbol]."""
    t: pd.DatetimeIndex
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    vol: pd.DataFrame

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    @classmethod
    def load(cls, symbols, interval="1h") -> "Panel":
        frames = {s: load(s, interval).set_index("t") for s in symbols}
        idx = sorted(set().union(*[f.index for f in frames.values()]))
        idx = pd.DatetimeIndex(idx)
        get = lambda col: pd.DataFrame({s: f[col].reindex(idx) for s, f in frames.items()})
        return cls(idx, get("open"), get("high"), get("low"), get("close"),
                   get("quote_volume"))


@dataclass
class Spec:
    """What a strategy wants. Arrays are [time x symbol] bools at bar close."""
    entry: np.ndarray
    exit: np.ndarray | None = None
    stop: float | None = None          # fraction below entry
    tp: float | None = None            # fraction above entry
    trail: float | None = None         # fraction below highest high since entry
    trail_atr: tuple | None = None     # (atr array [time x symbol], multiple)
    max_hold: int | None = None        # bars
    priority: np.ndarray | None = None  # higher = filled first when cash is short


@dataclass
class Result:
    final_value: float
    ret: float
    cooked: bool
    cooked_at: str | None
    max_dd: float
    trades: int
    win_rate: float
    avg_trade: float          # mean net return per trade, after all costs
    profit_factor: float
    exposure: float           # fraction of bars with a position open
    costs: float              # fees + tips paid
    curve: np.ndarray = field(repr=False, default=None)


def backtest(panel: Panel, spec: Spec, cfg: WalletConfig, start: int, end: int,
             keep_curve: bool = False) -> Result:
    o, h, l, c = (x.to_numpy() for x in (panel.open, panel.high, panel.low, panel.close))
    syms = panel.symbols
    entry = spec.entry
    exit_ = spec.exit
    atr = spec.trail_atr[0] if spec.trail_atr else None
    prio = spec.priority

    w = MockWallet(":memory:", cfg)
    trader = PaperTrader(w)
    pos: dict[int, dict] = {}
    pend_in: list[int] = []
    pend_out: set[int] = set()
    curve = np.full(end - start, np.nan)
    bars_in = 0
    cooked = False
    cooked_at = None

    def sell(j, price, why):
        trader.sell(pos[j]["pid"], max(price, 0.0), reason=why)
        del pos[j]

    i = start
    try:
        for i in range(start, end):
            # 1) exits, then entries, at the open
            for j in list(pend_out):
                if j in pos and not np.isnan(o[i, j]):
                    sell(j, o[i, j], "signal")
            pend_out.clear()
            for j in pend_in:
                if j in pos or np.isnan(o[i, j]) or o[i, j] <= 0:
                    continue
                p = trader.buy(syms[j], o[i, j])
                if p is None:
                    break  # out of cash
                pos[j] = {"pid": p["position_id"], "px": o[i, j], "i": i, "peak": o[i, j]}
            pend_in = []

            # 2) intrabar stops / targets
            for j in list(pos):
                P = pos[j]
                if np.isnan(l[i, j]):
                    continue
                lvl = -np.inf
                if spec.stop:
                    lvl = max(lvl, P["px"] * (1 - spec.stop))
                if spec.trail:
                    lvl = max(lvl, P["peak"] * (1 - spec.trail))
                if atr is not None and not np.isnan(atr[i - 1, j]):
                    lvl = max(lvl, P["peak"] - spec.trail_atr[1] * atr[i - 1, j])
                if lvl > -np.inf and l[i, j] <= lvl:
                    sell(j, min(o[i, j], lvl), "stop")
                    continue
                if spec.tp:
                    tgt = P["px"] * (1 + spec.tp)
                    if h[i, j] >= tgt:
                        sell(j, max(o[i, j], tgt), "target")
                        continue
                P["peak"] = max(P["peak"], h[i, j])

            # 3) mark to the close (can trip the kill switch)
            if pos:
                bars_in += 1
                trader.mark({syms[j]: c[i, j] for j in pos if not np.isnan(c[i, j])})

            # 4) signals at the close for the next open
            for j, P in pos.items():
                if (exit_ is not None and exit_[i, j]) or (
                        spec.max_hold and i - P["i"] + 1 >= spec.max_hold):
                    pend_out.add(j)
            cand = [j for j in np.flatnonzero(entry[i]) if j not in pos]
            if prio is not None and len(cand) > 1:
                cand.sort(key=lambda j: -prio[i, j])
            pend_in = cand
            curve[i - start] = float(w.balance + w.open_position_value() + w.vault)
    except RunCooked as e:
        cooked = True
        cooked_at = str(panel.t[i])
        curve[i - start:] = float(w.balance + w.vault)

    # Close anything still open at the last close so the result is realised.
    if not cooked:
        for j in list(pos):
            px = c[end - 1, j]
            try:
                sell(j, 0.0 if np.isnan(px) else px, "end of test")
            except RunCooked:
                cooked, cooked_at = True, str(panel.t[end - 1])
                break
        curve[-1] = float(w.balance + w.vault)

    rows = w.conn.execute(
        "SELECT pnl, cost_basis FROM positions WHERE status != 'open'").fetchall()
    rets = np.array([r["pnl"] / r["cost_basis"] for r in rows]) if rows else np.array([])
    gains = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    losses = -sum(r["pnl"] for r in rows if r["pnl"] < 0)
    costs = w.conn.execute(
        "SELECT COALESCE(SUM(-amount),0) s FROM ledger WHERE kind IN ('fee','tip')"
    ).fetchone()["s"] / 1e6
    final = float(w.balance + w.vault + w.open_position_value())
    start_bal = float(cfg.start_balance)
    cv = curve[~np.isnan(curve)]
    peak = np.maximum.accumulate(cv) if len(cv) else np.array([start_bal])
    mdd = float(np.max((peak - cv) / peak)) if len(cv) else 0.0
    w.close()
    return Result(
        final_value=final, ret=final / start_bal - 1, cooked=cooked, cooked_at=cooked_at,
        max_dd=mdd, trades=len(rets),
        win_rate=float((rets > 0).mean()) if len(rets) else 0.0,
        avg_trade=float(rets.mean()) if len(rets) else 0.0,
        profit_factor=float(gains / losses) if losses else (float("inf") if gains else 0.0),
        exposure=bars_in / max(end - start, 1), costs=costs,
        curve=curve if keep_curve else None)

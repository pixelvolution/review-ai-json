"""Portfolio backtester that trades through the real MockWallet.

One wallet, a basket of coins. Every fill goes through ``PaperTrader`` so
fees, slippage, tips, equity-based sizing, the cup rule and the kill switch
apply exactly as they will in paper trading.

Timing (no lookahead):
  * signals are computed on bar i's close and filled at bar i+fill_delay's
    open (fill_delay >= 1);
  * stops/targets are checked intrabar on bar i+1 onward; if the low hits
    the stop and the high hits the target in the same bar, the stop wins;
  * a gap through a stop fills at the (worse) open.

Honeypots: with ``honeypot_rate`` > 0, each entry is a honeypot with that
probability, decided by hashing (seed, pair, entry time). Any sell of a
honeypot fails: total loss plus the burned tip. Because the draw depends only
on the pair and entry time, two configs that take the same trade hit the same
honeypot, so comparisons aren't decided by luck.
"""

from __future__ import annotations

import hashlib
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
    honeypots: int = 0
    curve: np.ndarray = field(repr=False, default=None)
    trade_log: list = field(repr=False, default=None)


def is_honeypot(seed: int, symbol: str, ts, rate: float) -> bool:
    if rate <= 0:
        return False
    h = hashlib.sha256(f"{seed}|{symbol}|{pd.Timestamp(ts).isoformat()}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64 < rate


def backtest(panel: Panel, spec: Spec, cfg: WalletConfig, start: int, end: int,
             keep_curve: bool = False, fill_delay: int = 1, honeypot_rate: float = 0.0,
             honeypot_seed: int = 0) -> Result:
    if fill_delay < 1:
        raise ValueError("fill_delay must be >= 1 (a signal can't fill on its own bar)")
    o, h, l, c = (x.to_numpy() for x in (panel.open, panel.high, panel.low, panel.close))
    syms = panel.symbols
    entry = spec.entry
    exit_ = spec.exit
    atr = spec.trail_atr[0] if spec.trail_atr else None
    prio = spec.priority

    w = MockWallet(":memory:", cfg)
    trader = PaperTrader(w)
    pos: dict[int, dict] = {}
    pend_in: list[tuple[int, int]] = []   # (bar it fills on, symbol index)
    pend_out: dict[int, int] = {}          # symbol index -> bar it fills on
    trades: list[dict] = []
    honeypots = 0
    curve = np.full(end - start, np.nan)
    bars_in = 0
    cooked = False
    cooked_at = None

    def sell(j, price, why, bar):
        P = pos[j]
        if P["honeypot"]:
            trader.sell(P["pid"], failed=True, reason=f"honeypot ({why})")
            price = 0.0
        else:
            trader.sell(P["pid"], max(price, 0.0), reason=why)
        trades.append({"symbol": syms[j], "entry_t": panel.t[P["i"]], "entry_px": P["px"],
                       "exit_t": panel.t[bar], "exit_px": price, "reason": why,
                       "honeypot": P["honeypot"]})
        del pos[j]

    i = start
    try:
        for i in range(start, end):
            # 1) exits, then entries, that are due at this open
            for j, due in list(pend_out.items()):
                if j not in pos:
                    del pend_out[j]
                elif due <= i and not np.isnan(o[i, j]):
                    sell(j, o[i, j], "signal", i)
                    del pend_out[j]
            waiting, broke = [], False
            for due, j in pend_in:
                if due > i:
                    waiting.append((due, j))
                    continue
                if broke or j in pos or np.isnan(o[i, j]) or o[i, j] <= 0:
                    continue
                p = trader.buy(syms[j], o[i, j])
                if p is None:
                    broke = True  # out of cash: drop the rest due now
                    continue
                hp = is_honeypot(honeypot_seed, syms[j], panel.t[i], honeypot_rate)
                honeypots += hp
                pos[j] = {"pid": p["position_id"], "px": o[i, j], "i": i, "peak": o[i, j],
                          "honeypot": hp}
            pend_in = waiting

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
                    sell(j, min(o[i, j], lvl), "stop", i)
                    continue
                if spec.tp:
                    tgt = P["px"] * (1 + spec.tp)
                    if h[i, j] >= tgt:
                        sell(j, max(o[i, j], tgt), "target", i)
                        continue
                P["peak"] = max(P["peak"], h[i, j])

            # 3) mark to the close (can trip the kill switch)
            if pos:
                bars_in += 1
                trader.mark({syms[j]: c[i, j] for j in pos if not np.isnan(c[i, j])})

            # 4) signals at this close, filled fill_delay bars later
            for j, P in pos.items():
                if j not in pend_out and (
                        (exit_ is not None and exit_[i, j]) or (
                        spec.max_hold and i - P["i"] + 1 >= spec.max_hold)):
                    pend_out[j] = i + fill_delay
            queued = {j for _, j in pend_in}
            cand = [j for j in np.flatnonzero(entry[i]) if j not in pos and j not in queued]
            if prio is not None and len(cand) > 1:
                cand.sort(key=lambda j: -prio[i, j])
            pend_in.extend((i + fill_delay, j) for j in cand)
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
                sell(j, 0.0 if np.isnan(px) else px, "end of test", end - 1)
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
        exposure=bars_in / max(end - start, 1), costs=costs, honeypots=honeypots,
        curve=curve if keep_curve else None, trade_log=trades)

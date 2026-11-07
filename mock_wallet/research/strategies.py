"""Strategy library. Each builder takes a Panel and params, returns a Spec.

Long-only (the wallet buys and sells spot; it can't short). All indicators are
causal: bar i only uses data up to bar i's close.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from engine import Panel, Spec


# ---------------------------------------------------------------- indicators

def sma(df, n):
    return df.rolling(n, min_periods=n).mean()


def ema(df, n):
    return df.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(df, n):
    d = df.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(p: Panel, n):
    prev = p.close.shift(1)
    tr = pd.concat([p.high - p.low, (p.high - prev).abs(), (p.low - prev).abs()]).groupby(
        level=0).max()
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def cross_up(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_dn(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def btc_regime(p: Panel, n, btc_close: pd.Series):
    """True for every symbol when BTC closes above its n-bar SMA."""
    ok = btc_close > btc_close.rolling(n, min_periods=n).mean()
    return pd.DataFrame(np.repeat(ok.to_numpy()[:, None], p.close.shape[1], axis=1),
                        index=p.close.index, columns=p.close.columns)


def B(df) -> np.ndarray:
    return df.fillna(False).astype(bool).to_numpy()


# ---------------------------------------------------------------- strategies
# Each: (panel, btc_close, **params) -> Spec

def random_entry(p, btc, prob, hold, seed=0):
    rng = np.random.default_rng(seed)
    e = rng.random(p.close.shape) < prob
    return Spec(entry=e & p.close.notna().to_numpy(), max_hold=hold)


def sma_cross(p, btc, fast, slow):
    f, s = sma(p.close, fast), sma(p.close, slow)
    return Spec(entry=B(cross_up(f, s)), exit=B(cross_dn(f, s)))


def ema_cross(p, btc, fast, slow, stop=None):
    f, s = ema(p.close, fast), ema(p.close, slow)
    return Spec(entry=B(cross_up(f, s)), exit=B(cross_dn(f, s)), stop=stop)


def macd(p, btc, scale, regime=None):
    m = ema(p.close, 12 * scale) - ema(p.close, 26 * scale)
    sig = ema(m, 9 * scale)
    e = cross_up(m, sig)
    if regime:
        e &= btc_regime(p, regime, btc)
    return Spec(entry=B(e), exit=B(cross_dn(m, sig)))


def donchian(p, btc, n, m, regime=None):
    e = p.close > p.high.rolling(n).max().shift(1)
    if regime:
        e &= btc_regime(p, regime, btc)
    x = p.close < p.low.rolling(m).min().shift(1)
    return Spec(entry=B(e), exit=B(x))


def atr_breakout(p, btc, n, mult, regime=None):
    e = p.close > p.high.rolling(n).max().shift(1)
    if regime:
        e &= btc_regime(p, regime, btc)
    return Spec(entry=B(e), trail_atr=(atr(p, 14).to_numpy(), mult))


def volume_breakout(p, btc, n, vmult, trail):
    e = (p.close > p.high.rolling(n).max().shift(1)) & (
        p.vol > vmult * p.vol.rolling(24 * 7).mean())
    return Spec(entry=B(e), trail=trail)


def momentum(p, btc, look, thr, hold, stop):
    r = p.close / p.close.shift(look) - 1
    return Spec(entry=B(r > thr), max_hold=hold, stop=stop)


def pump_chase(p, btc, jump, tp, sl, hold):
    """Buy a candle that just pumped `jump` — what many memecoin bots do."""
    r = p.close / p.open - 1
    return Spec(entry=B(r > jump), tp=tp, stop=sl, max_hold=hold)


def volume_spike(p, btc, vmult, ret, tp, sl):
    e = (p.vol > vmult * p.vol.rolling(48).mean()) & (p.close / p.open - 1 > ret)
    return Spec(entry=B(e), tp=tp, stop=sl, max_hold=48)


def rsi_revert(p, btc, n, lo, hi, stop):
    r = rsi(p.close, n)
    return Spec(entry=B(r < lo), exit=B(r > hi), stop=stop)


def connors_rsi2(p, btc, trend, lo, stop):
    e = (p.close > sma(p.close, trend)) & (rsi(p.close, 2) < lo)
    return Spec(entry=B(e), exit=B(p.close > sma(p.close, 5)), stop=stop)


def bb_revert(p, btc, n, k, stop):
    mid = sma(p.close, n)
    sd = p.close.rolling(n).std()
    return Spec(entry=B(p.close < mid - k * sd), exit=B(p.close > mid), stop=stop)


def bb_breakout(p, btc, n, k, regime=None):
    mid = sma(p.close, n)
    sd = p.close.rolling(n).std()
    e = p.close > mid + k * sd
    if regime:
        e &= btc_regime(p, regime, btc)
    return Spec(entry=B(e), exit=B(p.close < mid))


def squeeze(p, btc, n, pct, trail):
    mid = sma(p.close, n)
    sd = p.close.rolling(n).std()
    bw = (4 * sd / mid)
    tight = bw <= bw.rolling(24 * 7).quantile(pct)
    e = tight.shift(1).fillna(False).astype(bool) & (p.close > mid + 2 * sd)
    return Spec(entry=B(e), trail=trail)


def dip_buy(p, btc, look, drop, tp, sl, hold):
    dd = p.close / p.close.rolling(look).max() - 1
    return Spec(entry=B(dd < -drop), tp=tp, stop=sl, max_hold=hold)


def trend_pullback(p, btc, trend, tp, sl):
    e = (p.close > sma(p.close, trend)) & (p.close < ema(p.close, 20)) & (rsi(p.close, 14) < 40)
    return Spec(entry=B(e), tp=tp, stop=sl, max_hold=72)


def red_streak(p, btc, n, tp, sl):
    red = (p.close < p.open).astype(int)
    e = red.rolling(n).sum() == n
    return Spec(entry=B(e), tp=tp, stop=sl, max_hold=24)


def rotation(p, btc, look, k, every, regime=None):
    """Hold the k strongest coins by `look`-bar return; rebalance every `every` bars."""
    r = (p.close / p.close.shift(look) - 1)
    rank = r.rank(axis=1, ascending=False)
    top = (rank <= k) & (r > 0)
    if regime:
        top &= btc_regime(p, regime, btc)
    reb = np.zeros(len(p.t), dtype=bool)
    reb[::every] = True
    topn = B(top)
    entry = topn & reb[:, None]
    exit_ = (~topn) & reb[:, None]
    return Spec(entry=entry, exit=exit_, priority=r.fillna(-9).to_numpy())


def trend_hold(p, btc, n, regime=None):
    """Own every coin above its n-bar SMA; leave when it drops below."""
    above = p.close > sma(p.close, n)
    e = cross_up(p.close, sma(p.close, n))
    if regime:
        e &= btc_regime(p, regime, btc)
    return Spec(entry=B(e), exit=B(~above))


# ---------------------------------------------------------------- grid

def grid(**axes):
    keys = list(axes)
    return [dict(zip(keys, v)) for v in itertools.product(*axes.values())]


STRATEGIES = {
    "random_entry": (random_entry, grid(prob=[0.002, 0.01], hold=[12, 48], seed=[1])),
    "sma_cross": (sma_cross, grid(fast=[12, 24, 50], slow=[100, 200])),
    "ema_cross": (ema_cross, grid(fast=[9, 21], slow=[55, 144], stop=[None, 0.15])),
    "macd": (macd, grid(scale=[1, 4], regime=[None, 200])),
    "donchian": (donchian, grid(n=[24, 72, 168], m=[12, 48], regime=[None, 200])),
    "atr_breakout": (atr_breakout, grid(n=[48, 168], mult=[2, 4], regime=[None, 200])),
    "volume_breakout": (volume_breakout, grid(n=[24, 72], vmult=[2, 4], trail=[0.1, 0.2])),
    "momentum": (momentum, grid(look=[24, 72], thr=[0.1, 0.3], hold=[24, 72], stop=[0.1])),
    "pump_chase": (pump_chase, grid(jump=[0.05, 0.1], tp=[0.1, 0.3], sl=[0.05, 0.1], hold=[24])),
    "volume_spike": (volume_spike, grid(vmult=[3, 6], ret=[0.02, 0.05], tp=[0.1], sl=[0.05])),
    "rsi_revert": (rsi_revert, grid(n=[14], lo=[20, 30], hi=[50, 70], stop=[0.1, 0.2])),
    "connors_rsi2": (connors_rsi2, grid(trend=[100, 200], lo=[5, 10], stop=[None, 0.1])),
    "bb_revert": (bb_revert, grid(n=[20, 50], k=[2, 3], stop=[0.1, 0.2])),
    "bb_breakout": (bb_breakout, grid(n=[20, 50], k=[2, 3], regime=[None, 200])),
    "squeeze": (squeeze, grid(n=[20], pct=[0.1, 0.2], trail=[0.08, 0.15])),
    "dip_buy": (dip_buy, grid(look=[24, 72], drop=[0.15, 0.3], tp=[0.1, 0.2], sl=[0.1], hold=[72])),
    "trend_pullback": (trend_pullback, grid(trend=[100, 200], tp=[0.05, 0.1], sl=[0.05, 0.1])),
    "red_streak": (red_streak, grid(n=[3, 5], tp=[0.03, 0.06], sl=[0.03, 0.06])),
    "rotation": (rotation, grid(look=[24, 168], k=[3, 5], every=[24, 168], regime=[None, 200])),
    "trend_hold": (trend_hold, grid(n=[100, 200, 500], regime=[None, 200])),
}

"""Wash-trade metrics and score from trade-level records.

Only trades at or before ``as_of`` are used. Components (each 0..1, higher =
more wash-like):
  trades_per_wallet   few wallets making many trades
  round_trip          share of wallets that both bought and sold within round_trip_s
  concentration       share of USD volume from the top 3 wallets
  size_regularity     trade sizes suspiciously alike (low coefficient of variation)
  timing_regularity   trades spaced suspiciously evenly
  funding_cluster     unavailable: needs wallet funding history (RPC key)
The score is the weighted mean of the available components.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

UNAVAILABLE = {"funding_cluster": "needs wallet funding history (RPC transaction history refused)"}


def _cv(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mean = statistics.fmean(xs)
    return statistics.pstdev(xs) / mean if mean else None


def metrics(trades: list[dict], as_of: float, round_trip_s: float) -> dict:
    """``trades``: dicts with t (unix), wallet, kind ('buy'|'sell'), volume_usd."""
    ts = sorted((t for t in trades if t["t"] <= as_of), key=lambda t: t["t"])
    n = len(ts)
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in ts:
        by_wallet[t["wallet"]].append(t)
    wallets = len(by_wallet)
    rt = 0
    for w_trades in by_wallet.values():
        buys = [t["t"] for t in w_trades if t["kind"] == "buy"]
        sells = [t["t"] for t in w_trades if t["kind"] == "sell"]
        if any(abs(s - b) <= round_trip_s for b in buys for s in sells):
            rt += 1
    vols = [t["volume_usd"] for t in ts if t["volume_usd"] is not None]
    total = sum(vols)
    wallet_vol = sorted((sum(x["volume_usd"] or 0 for x in v) for v in by_wallet.values()),
                        reverse=True)
    gaps = [b["t"] - a["t"] for a, b in zip(ts, ts[1:])]
    return {
        "n_trades": n, "unique_wallets": wallets,
        "trades_per_wallet": n / wallets if wallets else None,
        "round_trip_share": rt / wallets if wallets else None,
        "top3_volume_share": sum(wallet_vol[:3]) / total if total else None,
        "size_cv": _cv(vols), "interval_cv": _cv(gaps),
        "first_trade_t": ts[0]["t"] if ts else None, "last_trade_t": ts[-1]["t"] if ts else None,
    }


def components(m: dict, cfg: dict) -> dict:
    def clip(x):
        return max(0.0, min(1.0, x))
    c = {}
    if m["trades_per_wallet"] is not None:
        c["trades_per_wallet"] = clip((m["trades_per_wallet"] - 1) / (cfg["tpw_scale"] - 1))
    if m["round_trip_share"] is not None:
        c["round_trip"] = m["round_trip_share"]
    if m["top3_volume_share"] is not None:
        c["concentration"] = clip((m["top3_volume_share"] - cfg["top3_floor"])
                                  / (1 - cfg["top3_floor"]))
    if m["size_cv"] is not None:
        c["size_regularity"] = clip(1 - m["size_cv"] / cfg["cv_scale"])
    if m["interval_cv"] is not None:
        c["timing_regularity"] = clip(1 - m["interval_cv"] / cfg["cv_scale"])
    return c


def score(trades: list[dict], as_of: float, cfg: dict) -> dict:
    """Returns {status, score, flagged, metrics, components, unavailable}.
    status: ok | insufficient_trades."""
    m = metrics(trades, as_of, cfg["round_trip_s"])
    if m["n_trades"] < cfg["min_trades"]:
        return {"status": "insufficient_trades", "score": None, "flagged": None, "metrics": m,
                "components": {}, "unavailable": UNAVAILABLE}
    comp = components(m, cfg)
    w = cfg["weights"]
    used = {k: v for k, v in comp.items() if w.get(k, 0) > 0}
    s = sum(w[k] * v for k, v in used.items()) / sum(w[k] for k in used) if used else None
    return {"status": "ok", "score": s, "flagged": s is not None and s >= cfg["threshold"],
            "metrics": m, "components": comp, "unavailable": UNAVAILABLE}

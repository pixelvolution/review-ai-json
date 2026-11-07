"""Early features, computed only from data timestamped at or before a cutoff.

``data_cutoff`` is never later than first_seen + window. The only query of
scanner observations filters on ``observed_at <= data_cutoff``, and live
reads (the bonding curve) are only used when made before the cutoff.
"""

from __future__ import annotations

from datetime import datetime

from .cohort import iso, parse_ts, source_for
from .curve import CurveState

SOL_MINT = "So11111111111111111111111111111111111111112"

DEFERRED = {
    "unique_wallets": "study phase 2b (trade records, after main Phase 3)",
    "trades_per_wallet": "study phase 2b (trade records, after main Phase 3)",
    "wash_score": "study phase 2b (main Phase 3 wash filter)",
    "bundled_buys": "study phase 2b (trade records)",
    "has_website": "study phase 2b (socials source not chosen)",
    "has_twitter": "study phase 2b (socials source not chosen)",
    "has_telegram": "study phase 2b (socials source not chosen)",
    "top_holder_share": "needs an RPC key (public RPC refuses getTokenLargestAccounts)",
    "creator_prior": "needs an RPC key (creator history)",
    "creator_death_rate": "needs an RPC key (creator history)",
    "creator_funding": "needs an RPC key (creator history)",
}
CURVE_FIELDS = ("curve_read_at", "curve_complete", "curve_price_sol", "curve_virtual_sol",
                "curve_virtual_tokens", "curve_real_tokens", "curve_real_sol",
                "curve_consistent")


def compute(conn, cfg: dict, member, window_min: int, data_cutoff: datetime,
            curve: CurveState | None, curve_read_at: datetime | None,
            curve_missing: str | None) -> dict:
    first_seen = parse_ts(member["first_seen_at"])
    limit = first_seen.timestamp() + window_min * 60
    if data_cutoff.timestamp() > limit + 1e-6:
        raise ValueError("data_cutoff after first_seen + window: look-ahead refused")
    cut = iso(data_cutoff)
    rows = conn.execute(
        "SELECT * FROM pairs_seen WHERE pair_address = ? AND observed_at <= ? ORDER BY id",
        (member["pair_address"], cut)).fetchall()
    missing = dict(DEFERRED)
    f: dict = {"n_obs": len(rows)}
    if rows:
        first, last = rows[0], rows[-1]
        f.update(liquidity_usd=last["liquidity_usd"], price_usd=last["price_usd"],
                 vol_m5=last["vol_m5"], vol_h1=last["vol_h1"], buys_h1=last["buys_h1"],
                 sells_h1=last["sells_h1"])
        if first["price_usd"] and last["price_usd"]:
            f["price_change"] = last["price_usd"] / first["price_usd"] - 1
        if last["buys_h1"] is not None and last["sells_h1"]:
            f["buy_sell_ratio"] = last["buys_h1"] / last["sells_h1"]
        elif last["sells_h1"] == 0:
            missing["buy_sell_ratio"] = "no sells yet (ratio undefined)"
    else:
        missing["observations"] = "no scanner observation before the cutoff"

    # Graduation: the mint appeared in a regular pool by the cutoff, or the curve says complete.
    if member["source"] != "pool":
        pool = None
        for r in conn.execute(
                "SELECT pair_address, dex, first_seen_at FROM pairs WHERE base_mint = ? AND "
                "pair_address != ? AND first_seen_at <= ? ORDER BY first_seen_at",
                (member["mint"], member["pair_address"], cut)):
            if source_for(cfg, r["dex"]) == "pool":
                pool = r
                break
        complete = bool(curve and curve.readable and curve.complete)
        f["graduated"] = int(pool is not None or complete)
        if pool is not None:
            f["minutes_to_graduate"] = (
                parse_ts(pool["first_seen_at"]) - first_seen).total_seconds() / 60
        elif complete:
            missing["minutes_to_graduate"] = "curve complete but no pool seen yet"

    if curve is not None and curve.readable:
        f.update(curve_read_at=iso(curve_read_at), curve_complete=int(curve.complete),
                 curve_price_sol=curve.price_sol, curve_virtual_sol=curve.virtual_sol,
                 curve_virtual_tokens=curve.virtual_tokens, curve_real_tokens=curve.real_tokens,
                 curve_real_sol=curve.real_sol)
        native = rows[-1]["price_native"] if rows else None
        quote = conn.execute("SELECT quote_mint FROM pairs WHERE pair_address = ?",
                             (member["pair_address"],)).fetchone()
        if quote is None or quote["quote_mint"] != SOL_MINT:
            # The curve price is in SOL; a non-SOL pair's price can't be compared.
            missing["curve_consistent"] = "pair not priced in SOL"
        elif curve.price_sol and native:
            f["curve_consistent"] = int(
                abs(curve.price_sol / native - 1) <= cfg["study"]["curve_price_tol"])
    elif member["source"] == "pumpfun_curve":
        reason = curve_missing or "curve account unreadable"
        for k in CURVE_FIELDS:
            missing[k] = reason
    missing["curve_progress"] = "not derived: curve parameters vary between pump.fun variants"
    missing["creator_wallet"] = "not derived: curve layout for creator not verified"
    f["missing"] = missing
    return f

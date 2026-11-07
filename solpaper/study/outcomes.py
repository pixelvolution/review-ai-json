"""Outcome checkpoints and labels (study phase 3).

At first_seen + 1h, 6h, 24h, 72h and 7d, look the launch's token up on
DexScreener (every pair it trades in) and record liquidity, 24 h volume,
price, the peak multiple observed so far, graduation and a label:

  dead        liquidity and trading observed collapsed (positive evidence only)
  graduated   a bonding-curve token that now trades in a regular pool
  survived    liquidity (or, with no liquidity figure, volume and price) held up
  flopped     anything else with data
  unknown     no data: the provider returned nothing or the lookup failed.
              Never labelled dead for lack of data.

Outcomes live in their own append-only table and are never used to compute
features. The peak multiple uses only prices observed up to the checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import dex_label
from ..db import now_iso
from .cohort import iso, mark_graduate, parse_ts


def label(snap: dict, lc: dict, source: str) -> tuple[str, str]:
    """(label, reason) from a checkpoint snapshot."""
    liq, vol, ratio = snap["liquidity_usd"], snap["volume_h24"], snap["price_ratio"]
    if liq is None and snap["price_usd"] is None:
        return "unknown", "provider returned no usable data"
    low_vol = (vol or 0) < lc["dead_volume_h24_usd"]
    if liq is not None and liq < lc["dead_liquidity_usd"] and low_vol:
        return "dead", f"liquidity ${liq:,.0f} < ${lc['dead_liquidity_usd']:,} and 24h volume " \
                       f"${vol or 0:,.0f} < ${lc['dead_volume_h24_usd']:,}"
    if liq is None and ratio is not None and ratio <= lc["dead_price_ratio"] and low_vol:
        return "dead", f"no liquidity figure; price at {ratio:.0%} of first and 24h volume " \
                       f"${vol or 0:,.0f}"
    if snap["graduated"]:
        return "graduated", "bonding-curve token now trades in a regular pool"
    if liq is not None and liq >= lc["survived_liquidity_usd"]:
        return "survived", f"liquidity ${liq:,.0f} >= ${lc['survived_liquidity_usd']:,}"
    if liq is None and (vol or 0) >= lc["survived_volume_h24_usd"] and ratio is not None \
            and ratio >= lc["survived_price_ratio"]:
        return "survived", f"no liquidity figure; 24h volume ${vol:,.0f}, price at {ratio:.0%} of first"
    return "flopped", "data present, below the survival thresholds"


def peak_multiple(conn, pair: str, cutoff: float, price_now: float | None) -> tuple[float | None,
                                                                                  float | None]:
    """(peak / first price, first price) from observations at or before the cutoff."""
    cut = iso(datetime.fromtimestamp(cutoff, timezone.utc))
    rows = conn.execute("SELECT price_usd FROM pairs_seen WHERE pair_address = ? AND "
                        "observed_at <= ? AND price_usd IS NOT NULL ORDER BY id", (pair, cut)).fetchall()
    prices = [r[0] for r in rows]
    prices += [r[0] for r in conn.execute(
        "SELECT price_usd FROM launch_outcomes WHERE pair_address = ? AND price_usd IS NOT NULL "
        "AND captured_at <= ?", (pair, cut))]
    if price_now is not None:
        prices.append(price_now)
    if not prices or not rows:
        return None, None
    first = rows[0][0]
    return (max(prices) / first if first else None), first


def snapshot(cfg: dict, member, pairs: list) -> dict:
    curve_dexes = set(cfg["study"]["sources"])
    pools = [p for p in pairs if dex_label(cfg, p.dex) not in curve_dexes]
    liqs = [p.liquidity_usd for p in pairs if p.liquidity_usd is not None]
    best = max(pairs, key=lambda p: p.liquidity_usd or -1, default=None)
    vols = [p.vol.get("h24") for p in pairs if p.vol.get("h24") is not None]
    return {"liquidity_usd": max(liqs) if liqs else None,
            "volume_h24": sum(vols) if vols else None,
            "price_usd": best.price_usd if best else None,
            "graduated": int(member["source"] != "pool" and bool(pools))}


def process(conn, cfg: dict, ds, wall, limit: int = 300) -> tuple[int, int]:
    """Run due outcome tasks. Returns (labelled, gaps)."""
    sc = cfg["study"]
    names = {m: k for k, m in sc["checkpoints_min"].items()}
    now = wall()
    tasks = conn.execute("""
        SELECT q.id, q.window_min, q.due_at, q.attempts, c.* FROM study_queue q
        JOIN launch_cohort c USING (pair_address)
        WHERE q.status = 'pending' AND q.task = 'outcome' AND q.due_at <= ?
        ORDER BY q.due_at LIMIT ?""", (now, limit)).fetchall()
    done = gaps = 0
    by_mint: dict[str, list] = {}
    for t in tasks:
        by_mint.setdefault(t["mint"] or "", []).append(t)
    mints = [m for m in by_mint if m]
    found: dict[str, list] | None = {}
    for k in range(0, len(mints), 30):
        part = ds.token_pairs(mints[k:k + 30], bucket="study")
        if part is None:
            found = None
            break
        found.update(part)
    for mint, ts in by_mint.items():
        for t in ts:
            cp = names.get(t["window_min"], f"{t['window_min']}m")
            if found is None or not mint:
                if not mint or now - t["due_at"] > sc["outcome_retry_min"] * 60:
                    reason = "no mint recorded" if not mint else "lookups failed until the retry window ended"
                    _store(conn, t, cp, {"liquidity_usd": None, "volume_h24": None, "price_usd": None,
                                         "graduated": None, "price_ratio": None, "peak": None},
                           "unknown", reason)
                    _gap(conn, t, reason)
                    gaps += 1
                    done += 1
                else:
                    conn.execute("UPDATE study_queue SET attempts = attempts + 1, updated_at = ? "
                                 "WHERE id = ?", (now_iso(), t["id"]))
                continue
            pairs = found.get(mint) or []
            snap = snapshot(cfg, t, pairs)
            peak, first = peak_multiple(conn, t["pair_address"], now, snap["price_usd"])
            snap["peak"] = peak
            snap["price_ratio"] = (snap["price_usd"] / first) if (first and snap["price_usd"]) else None
            lab, why = label(snap, sc["labels"], t["source"])
            if not pairs:
                why = "provider returned no pair for this token"
                _gap(conn, t, why)
                gaps += 1
            _store(conn, t, cp, snap, lab, why)
            if snap["graduated"]:
                mark_graduate(conn, t["pair_address"])
            done += 1
    return done, gaps


def _store(conn, t, cp, snap, lab, why):
    conn.execute(
        "INSERT OR IGNORE INTO launch_outcomes (pair_address, checkpoint, captured_at, liquidity_usd, "
        "volume_h24, price_usd, peak_multiple, label, graduated, price_ratio, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (t["pair_address"], cp, now_iso(), snap["liquidity_usd"], snap["volume_h24"],
         snap["price_usd"], snap["peak"], lab, snap["graduated"], snap["price_ratio"], why))
    conn.execute("UPDATE study_queue SET status = 'done', attempts = attempts + 1, updated_at = ? "
                 "WHERE id = ?", (now_iso(), t["id"]))


def _gap(conn, t, reason):
    conn.execute("INSERT INTO study_gaps (pair_address, task, window_min, ts, reason) "
                 "VALUES (?, 'outcome', ?, ?, ?)", (t["pair_address"], t["window_min"], now_iso(),
                                                    reason))

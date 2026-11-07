"""Phase-1 summary of what the scanner captured.

    python -m solpaper.stats
"""

from __future__ import annotations

import sys

from .config import load_config
from .db import connect


def main() -> int:
    conn = connect(load_config()["db_path"])
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    print("== Capture")
    print(f"pairs seen: {one('SELECT COUNT(*) FROM pairs')}   "
          f"observations: {one('SELECT COUNT(*) FROM pairs_seen')}   "
          f"volume-spike observations: {one('SELECT COUNT(*) FROM pairs_seen WHERE volume_spike=1')}")
    print("first seen via: " + ", ".join(f"{r[0]} {r[1]}" for r in q(
        "SELECT first_source, COUNT(*) FROM pairs GROUP BY 1 ORDER BY 2 DESC")))
    print("observations by source: " + ", ".join(f"{r[0]} {r[1]}" for r in q(
        "SELECT source, COUNT(*) FROM pairs_seen GROUP BY 1 ORDER BY 2 DESC")))
    print("dexes: " + ", ".join(f"{r[0]} {r[1]}" for r in q(
        "SELECT dex, COUNT(*) FROM pairs GROUP BY 1 ORDER BY 2 DESC LIMIT 8")))

    print("\n== Requests by provider (sent, not cache hits)")
    for r in q("""SELECT provider, bucket, COUNT(*) n,
                  SUM(status=200) ok, SUM(status=429) r429, SUM(status IS NULL) neterr,
                  ROUND(AVG(ms)) ms, ROUND((julianday(MAX(ts))-julianday(MIN(ts)))*1440, 1) mins
                  FROM request_log GROUP BY 1, 2"""):
        rpm = r["n"] / r["mins"] if r["mins"] else float("nan")
        print(f"{r['provider']:13s} {r['bucket']:8s} sent {r['n']:4d}  200s {r['ok']:4d}  "
              f"429s {r['r429']:3d}  net errors {r['neterr']:3d}  avg {r['ms']:.0f} ms  "
              f"~{rpm:.1f} req/min")
    lim = q("SELECT provider, limits FROM request_log WHERE limits IS NOT NULL "
            "GROUP BY provider HAVING MAX(id)")
    for r in lim:
        print(f"  {r[0]} reported limits: {r[1]}")

    print("\n== Data gaps")
    gaps = q("SELECT * FROM data_gaps ORDER BY id")
    if not gaps:
        print("none")
    for g in gaps:
        print(f"{g['provider']}: {g['started_at']} -> {g['ended_at'] or 'ongoing'}  "
              f"{g['attempts']} failed attempts, last {g['last_status'] or g['last_error']}")

    print("\n== Sample of captured pairs (latest observation each)")
    rows = q("""SELECT p.name, p.dex, p.pair_created_at, s.liquidity_usd, s.vol_m5, s.vol_h1,
                s.buys_h1, s.sells_h1, s.price_usd, s.source, s.data_age_s, p.pair_address,
                (SELECT COUNT(*) FROM pairs_seen x WHERE x.pair_address = p.pair_address) n
                FROM pairs p JOIN pairs_seen s ON s.id = (
                    SELECT MAX(id) FROM pairs_seen WHERE pair_address = p.pair_address)
                ORDER BY p.first_seen_at DESC LIMIT 15""")
    fmt = lambda v, p="": "-" if v is None else f"{p}{v:,.0f}"
    print(f"{'pair':24s} {'dex':16s} {'created':17s} {'liq':>9s} {'vol5m':>8s} {'vol1h':>9s} "
          f"{'b/s 1h':>9s} {'obs':>4s} {'age':>5s} src")
    for r in rows:
        print(f"{(r['name'] or '')[:24]:24s} {(r['dex'] or '')[:16]:16s} "
              f"{(r['pair_created_at'] or '')[:16]:17s} {fmt(r['liquidity_usd'], '$'):>9s} "
              f"{fmt(r['vol_m5'], '$'):>8s} {fmt(r['vol_h1'], '$'):>9s} "
              f"{str(r['buys_h1'])+'/'+str(r['sells_h1']):>9s} {r['n']:>4d} "
              f"{fmt(r['data_age_s']):>5s} {r['source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

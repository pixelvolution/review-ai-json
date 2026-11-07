"""Study status: sample size and observation window first, always."""

from __future__ import annotations

import json


def discovery_saturation(conn, last_n: int = 30) -> tuple[int, int]:
    """Of the last N GeckoTerminal new_pools polls that returned 20 pools, how
    many were all never-seen pairs? If most were, launches are being missed."""
    rows = conn.execute("""
        SELECT substr(s.observed_at, 1, 16) m, COUNT(*) n,
               SUM(p.first_source = 'gt_new_pools'
                   AND substr(p.first_seen_at, 1, 16) = substr(s.observed_at, 1, 16)) new
        FROM pairs_seen s JOIN pairs p USING (pair_address)
        WHERE s.source = 'gt_new_pools' GROUP BY m ORDER BY m DESC LIMIT ?""",
                        (last_n,)).fetchall()
    full = [r for r in rows if r["n"] == 20]
    return sum(1 for r in full if r["new"] == 20), len(full)


def report(conn) -> str:
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    out = []
    n = one("SELECT COUNT(*) FROM launch_cohort")
    launches = one("SELECT COUNT(*) FROM launch_cohort WHERE status = 'launch'")
    win = q("SELECT MIN(first_seen_at), MAX(first_seen_at) FROM launch_cohort")[0]
    out.append(f"STUDY SAMPLE: {launches} launches ({n} pairs seen)   "
               f"window: {win[0] or '-'} -> {win[1] or '-'}")
    sat, polls = discovery_saturation(conn)
    if polls and sat / polls > 0.5:
        out.append(f"COVERAGE: UNKNOWN. {sat} of the last {polls} discovery polls returned 20/20 "
                   f"never-seen pools, so launches between polls are missed.")
    elif polls:
        out.append(f"coverage: discovery not saturated ({sat}/{polls} polls were 20/20 new)")
    out.append("Findings need days of data. Nothing here is a tradable edge.\n")

    out.append("== Cohort by source and status")
    for r in q("SELECT source, status, COUNT(*) k FROM launch_cohort GROUP BY 1, 2 ORDER BY 1, 2"):
        out.append(f"  {r['source']:14s} {r['status']:24s} {r['k']}")

    out.append("\n== Sampling (decided when each pair joins; independent of filters)")
    for r in q("""SELECT run_id, sample_rate, COUNT(*) n, SUM(sampled) s,
                  SUM(sampled_reason = 'random') rnd, SUM(sampled_reason = 'graduate') grad
                  FROM launch_cohort WHERE status = 'launch' GROUP BY run_id, sample_rate"""):
        out.append(f"  run {r['run_id']}: rate {r['sample_rate']:.0%}   launches {r['n']}   "
                   f"sampled {r['s']} (random {r['rnd']}, graduates {r['grad']})   "
                   f"realized random rate {(r['rnd'] or 0) / r['n']:.1%}")

    out.append("\n== Early features by window")
    for r in q("""SELECT q.window_min w, COUNT(*) tasks, SUM(q.status = 'done') done,
                  (SELECT COUNT(*) FROM study_gaps g WHERE g.window_min = q.window_min) gaps
                  FROM study_queue q WHERE q.task = 'features' GROUP BY q.window_min"""):
        rate = r["gaps"] / r["done"] if r["done"] else 0
        out.append(f"  {r['w']:>3} min: {r['done']}/{r['tasks']} captured, "
                   f"{r['gaps']} gaps ({rate:.0%})")
    for r in q("SELECT reason, COUNT(*) k FROM study_gaps GROUP BY reason ORDER BY k DESC"):
        out.append(f"      gap: {r['reason']} x{r['k']}")

    out.append("\n== Sample feature rows (newest)")
    for r in q("""SELECT c.source, f.window_min, f.n_obs, f.liquidity_usd, f.price_change,
                  f.buys_h1, f.sells_h1, f.graduated, f.curve_complete, f.curve_consistent,
                  f.missing, p.name
                  FROM launch_features f JOIN launch_cohort c USING (pair_address)
                  JOIN pairs p USING (pair_address) ORDER BY f.id DESC LIMIT 8"""):
        nmiss = len(json.loads(r["missing"]))
        liq = "-" if r["liquidity_usd"] is None else f"${r['liquidity_usd']:,.0f}"
        chg = "-" if r["price_change"] is None else f"{r['price_change']:+.0%}"
        out.append(f"  {(r['name'] or '')[:18]:18s} {r['source']:13s} {r['window_min']:>2}m "
                   f"obs {r['n_obs']:>2} liq {liq:>9} chg {chg:>6} b/s {r['buys_h1']}/"
                   f"{r['sells_h1']} grad {r['graduated']} curve_ok {r['curve_consistent']} "
                   f"({nmiss} fields NULL with reasons)")
    return "\n".join(out)

"""Phase 3 research: sample new pool launches, filter wash trading, bucket by
activity and concentration, and replay every rule variation with the honest
fill model.

    python -m solpaper.research                # fetch for a new sample, then report
    python -m solpaper.research --no-fetch     # report from data already stored
    python -m solpaper.research --sample 60 --seed abc

Every output starts with the sample size and the observation window. Results
are hypotheses: a rule that looks good here still has to pass validation on a
later window before it means anything.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import secrets
import statistics
import sys
import time
from datetime import datetime, timedelta

from .client import Http
from .config import dex_label, load_config
from .db import connect, now_iso
from .fills import ExitRule, Obs, model_from_config, simulate
from .providers.geckoterminal import GeckoTerminal
from .providers.solana_rpc import SolanaRpc
from .trades import store_trades, trades_as_of
from .wash import score as wash_score

ACTIVITY = [(0, 100, "<100 trades/24h"), (100, 1000, "100-999 trades/24h"),
            (1000, float("inf"), ">=1000 trades/24h")]


def ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def rules(cfg: dict) -> dict[str, ExitRule]:
    r = cfg["research"]
    out = {}
    for tp, sl, tm in itertools.product(r["rules"]["take_profit"], r["rules"]["stop_loss"],
                                        r["rules"]["time_stop_min"]):
        out[f"tp{int(tp * 100)}_sl{int(sl * 100)}_t{tm}m"] = ExitRule(
            tp, sl, tm * 60, r["liquidity_collapse"])
    return out


def population(conn, cfg: dict) -> list:
    """Pool launches (not bonding curves) with a liquidity figure at some point."""
    curve_dexes = set(cfg["study"]["sources"])
    max_age = timedelta(minutes=cfg["research"]["max_launch_age_min"])
    out = []
    for p in conn.execute("""SELECT p.* FROM pairs p WHERE EXISTS (
            SELECT 1 FROM pairs_seen s WHERE s.pair_address = p.pair_address
            AND s.liquidity_usd IS NOT NULL)"""):
        if dex_label(cfg, p["dex"]) in curve_dexes:
            continue
        created, seen = ts(p["pair_created_at"]), ts(p["first_seen_at"])
        if created and seen and seen - created <= max_age:
            out.append(p)
    return out


def draw_sample(pop: list, n: int, seed: str) -> list[tuple[float, object]]:
    keyed = []
    for p in pop:
        u = int.from_bytes(hashlib.sha256(f"{seed}|{p['pair_address']}".encode()).digest()[:8],
                           "big") / 2 ** 64
        keyed.append((u, p))
    return sorted(keyed, key=lambda x: x[0])[:n]


def entry_signal(conn, cfg: dict, pair) -> tuple[float, object] | None:
    """First observation at which the pair met the cheap eligibility rules.
    Uses only that observation and earlier facts (creation time, quote token)."""
    fc = cfg["filters"]
    if pair["quote_mint"] not in fc["allowed_quotes"]:
        return None
    created = ts(pair["pair_created_at"])
    for r in conn.execute("SELECT * FROM pairs_seen WHERE pair_address = ? ORDER BY id",
                          (pair["pair_address"],)):
        seen = ts(r["observed_at"])
        age_min = (seen - created).total_seconds() / 60
        if age_min < fc["min_age_min"] or age_min > fc["max_age_hours"] * 60:
            continue
        if r["liquidity_usd"] is not None and r["liquidity_usd"] >= fc["liquidity_floor_usd"] \
                and r["price_usd"]:
            return seen.timestamp(), r
    return None


def observations(conn, pair: str) -> list[Obs]:
    """Every reading, repeats included: a repeat is still an observation of the
    price at that moment (dropping them would invent gaps longer than the fill
    window). Staleness is a known limit of the source, noted in the report."""
    return [Obs(ts(r["observed_at"]).timestamp(), r["price_usd"], r["liquidity_usd"])
            for r in conn.execute("SELECT observed_at, price_usd, liquidity_usd FROM pairs_seen "
                                  "WHERE pair_address = ? ORDER BY id", (pair,))]


def activity_bucket(row) -> str:
    n = (row["buys_h24"] or 0) + (row["sells_h24"] or 0) if row["buys_h24"] is not None else None
    if n is None:
        return "unknown activity"
    return next(label for lo, hi, label in ACTIVITY if lo <= n < hi)


def concentration_bucket(share: float | None) -> str:
    if share is None:
        return "concentration unknown"
    return "top10 <30%" if share < 0.3 else "top10 30-60%" if share < 0.6 else "top10 >=60%"


def binom_tail(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p): how likely this many winners is by chance."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def fetch(conn, http: Http, cfg: dict, sample: list) -> dict:
    gt, rpc = GeckoTerminal(http, cfg), SolanaRpc(http, cfg)
    got = {"trades_ok": 0, "trades_failed": 0, "holders_ok": 0, "holders_failed": 0}
    for _, p in sample:
        tr = None
        for _attempt in range(4):          # batch job: wait out rate limits, then retry
            http.wait_if_backing_off(gt.name)
            tr = gt.trades(p["pair_address"], p["base_mint"])
            if tr is not None:
                break
        if tr is None:
            got["trades_failed"] += 1
        else:
            store_trades(conn, tr, time.time())
            got["trades_ok"] += 1
        h = rpc.holders(p["base_mint"], p["pair_address"]) if p["base_mint"] else None
        conn.execute(
            "INSERT INTO holder_checks (pair_address, mint, checked_at, top_share, "
            "top_share_ex_pair, largest_share, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["pair_address"], p["base_mint"] or "", time.time(),
             h and h["top_share"], h and h["top_share_ex_pair"], h and h["largest_share"],
             None if h else "RPC refused or failed (public RPC limits getTokenLargestAccounts)"))
        got["holders_ok" if h else "holders_failed"] += 1
    return got


def run(conn, cfg: dict, run_id: int, sample: list) -> list[dict]:
    model = model_from_config(cfg)
    rs = rules(cfg)
    size = cfg["research"]["size_usd"]
    out = []
    for _, p in sample:
        pa = p["pair_address"]
        sig = entry_signal(conn, cfg, p)
        h = conn.execute("SELECT top_share_ex_pair FROM holder_checks WHERE pair_address = ? "
                         "ORDER BY id DESC LIMIT 1", (pa,)).fetchone()
        conc = concentration_bucket(h["top_share_ex_pair"] if h else None)
        if sig is None:
            out.append({"pair": pa, "rule": None, "status": "never_eligible"})
            continue
        t_sig, srow = sig
        w = wash_score(trades_as_of(conn, pa, t_sig), t_sig, cfg["wash"])
        conn.execute(
            "INSERT INTO wash_checks (pair_address, checked_at, as_of, status, score, flagged, "
            "n_trades, metrics, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'research')",
            (pa, time.time(), t_sig, w["status"], w["score"],
             None if w["flagged"] is None else int(w["flagged"]), w["metrics"]["n_trades"],
             json.dumps({k: w[k] for k in ("metrics", "components", "unavailable")})))
        obs = observations(conn, pa)
        act = activity_bucket(srow)
        for rid, rule in rs.items():
            r = simulate(obs, t_sig, size, model, rule, pa)
            conn.execute(
                "INSERT INTO research_results (run_id, pair_address, rule_id, status, wash_status, "
                "wash_flagged, activity_bucket, concentration_bucket, entry_t, exit_t, exit_reason, "
                "honeypot, net, price_only_net) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, pa, rid, r.status, w["status"],
                 None if w["flagged"] is None else int(w["flagged"]), act, conc, r.entry_t,
                 r.exit_t, r.exit_reason, int(r.honeypot), r.net, r.price_only_net))
            out.append({"pair": pa, "rule": rid, "status": r.status, "net": r.net,
                        "price_only_net": r.price_only_net, "wash_status": w["status"],
                        "wash_flagged": w["flagged"], "activity": act, "concentration": conc})
    return out


def summarize(conn, cfg: dict, run_id: int, results: list[dict], sample: list) -> str:
    rp = cfg["research"]
    lines = []
    seen = [ts(p["first_seen_at"]) for _, p in sample]
    lines.append(f"RESEARCH SAMPLE: {len(sample)} pool launches   window: "
                 f"{min(seen) if seen else '-'} -> {max(seen) if seen else '-'}   run {run_id}")
    lines.append("Hypotheses only. Coverage of launches is unknown; concentration is measured "
                 "after the fact.\n")
    never = sum(1 for r in results if r["status"] == "never_eligible")
    pairs = {r["pair"] for r in results if r["rule"]}
    wash_by_pair = {r["pair"]: (r["wash_status"], r["wash_flagged"]) for r in results if r["rule"]}
    flagged = {p for p, (s, f) in wash_by_pair.items() if f}
    insufficient = {p for p, (s, f) in wash_by_pair.items() if s == "insufficient_trades"}
    clean = {p for p, (s, f) in wash_by_pair.items() if s == "ok" and not f}
    lines.append(f"Eligible at some point: {len(pairs)}   never eligible: {never}   wash-flagged: "
                 f"{len(flagged)}   too few trades to judge: {len(insufficient)}   clean: {len(clean)}")

    prior = conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        if r["rule"] and r["pair"] in clean and r["status"] == "closed" and r["net"] is not None:
            for bucket in ("all clean pairs", r["activity"], r["concentration"]):
                groups.setdefault((r["rule"], bucket), []).append(r)
    n_tests = prior + len(groups)
    alpha = 0.05 / max(n_tests, 1)
    lines.append(f"\nComparisons tried so far (all runs): {n_tests}. A rule only counts if its share "
                 f"of winners beats a coin flip at p < {alpha:.5f} (0.05 / {max(n_tests, 1)}).\n")
    lines.append(f"{'rule':18s} {'bucket':24s} {'pairs':>5s} {'net>0':>6s} {'median':>8s} "
                 f"{'total':>8s}  verdict")
    for (rid, bucket), rows in sorted(groups.items(), key=lambda kv: (kv[0][1] != "all clean pairs",
                                                                       kv[0][1], kv[0][0])):
        nets = [r["net"] for r in rows]
        k, n = sum(x > 0 for x in nets), len(nets)
        share, med = k / n, statistics.median(nets)
        if n < rp["min_pairs"]:
            verdict = "insufficient data / likely noise"
        elif binom_tail(k, n) < alpha and med > 0:
            verdict = "passes the bar (still needs validation on a later window)"
        else:
            verdict = "no edge"
        conn.execute("INSERT INTO research_hypotheses (run_id, ts, rule_id, bucket, n_pairs, "
                     "share_positive, median_net, verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, now_iso(), rid, bucket, n, share, med, verdict))
        if bucket == "all clean pairs" or n >= rp["min_pairs"]:
            lines.append(f"{rid:18s} {bucket:24s} {n:5d} {share:6.0%} {med:+8.2f} "
                         f"{sum(nets):+8.2f}  {verdict}")
    lines.append(f"\n(Bucket rows with fewer than {rp['min_pairs']} pairs are recorded but not "
                 f"printed: insufficient data.)")

    lines.append("\nWhat the wash filter saved us from (wash-flagged pairs, per rule):")
    for rid in rules(cfg):
        fr = [r for r in results if r["rule"] == rid and r["pair"] in flagged
              and r["status"] == "closed" and r["net"] is not None]
        if fr:
            looked = sum((r["price_only_net"] or 0) > 0 for r in fr)
            real = sum(r["net"] > 0 for r in fr)
            lines.append(f"  {rid:18s} {len(fr)} pairs: {looked} looked profitable on price alone, "
                         f"{real} were profitable after costs")
    gap_kinds: dict[str, int] = {}
    for r in results:
        if r["rule"] and r["status"] != "closed":
            gap_kinds[r["status"]] = gap_kinds.get(r["status"], 0) + 1
    lines.append(f"\nReplays that couldn't be priced honestly: {sum(gap_kinds.values())} "
                 f"{gap_kinds or ''}")
    lines.append("Source limit: DexScreener prices can be 30-60 s stale; repeated readings are "
                 "used as observations of the price at that moment.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase 3 research report.")
    ap.add_argument("--sample", type=int)
    ap.add_argument("--seed")
    ap.add_argument("--no-fetch", action="store_true", help="use stored trades/holders only")
    a = ap.parse_args(argv)
    cfg = load_config()
    conn = connect(cfg["db_path"])
    seed = a.seed or secrets.token_hex(8)
    n = a.sample or cfg["research"]["sample_size"]
    run_id = conn.execute("INSERT INTO research_runs (started_at, seed, config) VALUES (?, ?, ?)",
                          (now_iso(), seed, json.dumps({k: cfg[k] for k in
                                                        ("research", "wash", "fills", "filters")},
                                                       default=str))).lastrowid
    pop = population(conn, cfg)
    sample = draw_sample(pop, n, seed)
    for u, p in sample:
        conn.execute("INSERT INTO research_sample (run_id, pair_address, sample_u) VALUES (?, ?, ?)",
                     (run_id, p["pair_address"], u))
    print(f"population: {len(pop)} pool launches; sampled {len(sample)} (seed {seed})")
    if not a.no_fetch and sample:
        print("fetching trade records and holders (paced under the free limits; this takes a "
              "few minutes)...")
        print(fetch(conn, Http(conn, cfg), cfg, sample))
    results = run(conn, cfg, run_id, sample)
    print(summarize(conn, cfg, run_id, results, sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())

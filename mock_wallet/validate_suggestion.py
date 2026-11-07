"""Validate pending suggestions on paper money. Never applies anything.

    python validate_suggestion.py                 # every pending suggestion
    python validate_suggestion.py --id 3          # one suggestion
    python validate_suggestion.py --id 3 --retest # test again (counts as another test)
    python validate_suggestion.py --replay 2025-03-01 2025-09-01   # choose window A

For each suggestion, two tests, each candidate vs. the current live config
on the same window:
  1. REPLAY on window A, the window the suggestion was derived from;
  2. HOLDOUT on window B, which must not overlap window A.

Pass = beats the baseline net of costs by the required margin on BOTH
windows, with max drawdown no worse than baseline (plus tolerance), and at
least ``min_trades`` trades per run. The required margin grows with the
number of suggestions ever tested:

    required = min_margin + noise_sigma * sqrt(2 * ln(tests_tried))

sqrt(2 ln N) is roughly how far the best of N pure-noise results drifts above
zero, so the bar rises as more ideas get tried.

Passing only sets the status to 'validated' (or 'shadow' if shadow mode is on).
A person applies it with set_config.py.
"""

from __future__ import annotations

import argparse
import math
import sys

import pandas as pd

import control
import live_config
from replay import NoData, run_replay
from wallet import WalletConfig


def required_margin(n_tests: int, s: dict) -> float:
    n = max(n_tests, 1)
    return float(s["min_margin"]) + float(s["noise_sigma"]) * math.sqrt(2 * math.log(n))


def summarize(r) -> dict:
    return {"trades": r.trades, "ret": r.ret, "max_dd": r.max_dd, "win_rate": r.win_rate,
            "honeypots": r.honeypots, "cooked": r.cooked, "costs": r.costs}


def judge(windows: dict, s: dict, n_tests: int, min_trades: int | None = None):
    """Decide from per-window results. Pure: no I/O.

    ``windows`` maps name -> {"baseline": {...}, "candidate": {...}} or None
    (no data). Returns (verdict, reason, per-window detail).
    """
    need = int(min_trades if min_trades is not None else s["min_trades"])
    req = required_margin(n_tests, s)
    tol = float(s.get("max_dd_tolerance", 0))
    detail, short, wins = {}, [], []
    for name, w in windows.items():
        if w is None:
            short.append(f"{name}: no data")
            continue
        b, c = w["baseline"], w["candidate"]
        if b["trades"] < need or c["trades"] < need:
            short.append(f"{name}: {c['trades']} candidate / {b['trades']} baseline trades "
                         f"(need {need})")
        edge = c["ret"] - b["ret"]
        dd_ok = c["max_dd"] <= b["max_dd"] + tol
        won = edge >= req and dd_ok
        wins.append(won)
        detail[name] = {"edge": edge, "required": req, "dd_ok": dd_ok, "won": won}
    if short:
        return "insufficient data", "; ".join(short), detail
    if all(wins):
        return "validated", f"beat baseline by >= {req:+.2%} on both windows", detail
    if sum(wins) == 1:
        won = next(k for k, v in detail.items() if v["won"])
        return "likely noise", f"won only the {won} window", detail
    why = []
    for k, v in detail.items():
        if v["edge"] < req:
            why.append(f"{k}: {v['edge']:+.2%} vs required {req:+.2%}")
        if not v["dd_ok"]:
            why.append(f"{k}: drawdown worse than baseline")
    return "rejected", "; ".join(why), detail


def _window(x) -> tuple[pd.Timestamp, pd.Timestamp]:
    a, b = (pd.Timestamp(v) for v in x)
    return (a.tz_localize("UTC") if a.tzinfo is None else a,
            b.tz_localize("UTC") if b.tzinfo is None else b)


def overlaps(a, b) -> bool:
    (a0, a1), (b0, b1) = _window(a), _window(b)
    return a0 < b1 and b0 < a1


def _run_pair(base_cfg, cand_cfg, window, s, wcfg, market_db):
    try:
        b = run_replay(base_cfg, window, s, wcfg, market_db)
        c = run_replay(cand_cfg, window, s, wcfg, market_db)
    except NoData:
        return None
    return {"window": [str(x) for x in _window(window)],
            "baseline": summarize(b), "candidate": summarize(c)}


def validate(sid: int, db=None, market_db=None, live_config_path=None, settings=None,
             replay_window=None, retest=False, wallet_cfg=None) -> dict:
    s = settings or control.load_settings()
    sug = control.get_suggestion(sid, db)
    if sug["status"] != "pending" and not retest:
        raise ValueError(f"suggestion {sid} is '{sug['status']}', not pending (use --retest)")
    control.check_unlogged_edits(db, live_config_path)
    base = live_config.load(live_config_path)
    try:
        cand = live_config.with_override(base, sug["parameter"], sug["proposed_value"])
    except live_config.InvalidParameter as e:
        control.set_status(sid, "rejected", f"no longer valid for the live config: {e}", db)
        return control.get_suggestion(sid, db)
    if cand == base:
        control.set_status(sid, "rejected", "proposed value equals the live value", db)
        return control.get_suggestion(sid, db)

    win_a = replay_window or (sug["derived_start"], sug["derived_end"])
    win_b = s["holdout_window"]
    if overlaps(win_a, win_b):
        raise ValueError(f"holdout window {win_b} overlaps window A {win_a}; a suggestion "
                         f"must be validated on data it was not derived from")

    wcfg = wallet_cfg or WalletConfig.from_env()
    n = control.tests_tried(db) + 1
    windows = {"replay": _run_pair(base, cand, win_a, s, wcfg, market_db),
               "holdout": _run_pair(base, cand, win_b, s, wcfg, market_db)}
    verdict, reason, detail = judge(windows, s, n)
    evidence = {"windows": windows, "detail": detail, "baseline_config": base,
                "candidate_config": cand, "settings": s,
                "wallet_costs": {"fee_rate": str(wcfg.fee_rate),
                                 "slippage_rate": str(wcfg.slippage_rate),
                                 "tip": str(wcfg.tip)}}
    control.record_validation(sid, "test", n, required_margin(n, s), verdict, reason,
                              evidence, db)
    if verdict == "validated" and s.get("shadow", {}).get("enabled"):
        control.set_status(sid, "shadow", reason + "; now shadowing on live data", db,
                           shadow_started_at=control.now_iso())
    else:
        control.set_status(sid, verdict, reason, db)
    return control.get_suggestion(sid, db)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", type=int)
    ap.add_argument("--retest", action="store_true")
    ap.add_argument("--replay", nargs=2, metavar=("START", "END"),
                    help="window A to replay (default: the window the suggestion came from)")
    a = ap.parse_args(argv)
    ids = [a.id] if a.id else [x["id"] for x in control.list_suggestions("pending")]
    if not ids:
        print("no pending suggestions")
        return 0
    for sid in ids:
        try:
            r = validate(sid, replay_window=a.replay, retest=a.retest)
        except (ValueError, KeyError) as e:
            print(f"#{sid}: not tested: {e}", file=sys.stderr)
            continue
        print(f"#{sid} {r['parameter']} -> {r['proposed_value']}: {r['status'].upper()} "
              f"({r['status_reason']})")
    print(f"tests tried so far: {control.tests_tried()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

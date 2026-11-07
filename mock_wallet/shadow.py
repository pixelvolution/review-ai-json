"""Shadow mode: run a candidate in a second mock wallet on live data.

    python shadow.py      # run daily, e.g. after the morning report

Enabled with "shadow": {"enabled": true, "days": 14} in validation.json.
A suggestion that passes both windows goes to status 'shadow' instead of
'validated'. From then on, each run replays the candidate and the live
config side by side, each in its own mock wallet, over the bars the bot has
recorded in pairs_seen since the shadow started: the same pairs and the same
fill model. Both start fresh at the same balance.

When the shadow period is over:
  * beats the live config by min_margin, drawdown no worse -> 'validated'
  * otherwise -> 'rejected'
  * too few trades -> 'insufficient data'
Nothing is applied either way.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

import control
import live_config
from replay import NoData, run_replay
from validate_suggestion import summarize


def run_shadows(db=None, market_db=None, live_config_path=None, settings=None, now=None,
                wallet_cfg=None) -> list[dict]:
    s = settings or control.load_settings()
    sh = s.get("shadow", {})
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    out = []
    for sug in control.list_suggestions("shadow", db):
        started = pd.Timestamp(sug["shadow_started_at"])
        base = live_config.load(live_config_path)
        cand = live_config.with_override(base, sug["parameter"], sug["proposed_value"])
        try:
            b = summarize(run_replay(base, (started, now), s, wallet_cfg, market_db))
            c = summarize(run_replay(cand, (started, now), s, wallet_cfg, market_db))
        except NoData:
            out.append({"id": sug["id"], "status": "shadow", "note": "no live bars yet"})
            continue
        done = now - started >= timedelta(days=float(sh.get("days", 14)))
        evidence = {"window": [str(started), str(now)], "baseline": b, "candidate": c,
                    "complete": bool(done)}
        verdict, reason = "shadow", "in progress"
        if done:
            need = int(sh.get("min_trades", s["min_trades"]))
            edge = c["ret"] - b["ret"]
            if b["trades"] < need or c["trades"] < need:
                verdict = "insufficient data"
                reason = f"shadow: {c['trades']}/{b['trades']} trades (need {need})"
            elif edge >= float(s["min_margin"]) and \
                    c["max_dd"] <= b["max_dd"] + float(s.get("max_dd_tolerance", 0)):
                verdict, reason = "validated", f"shadow: beat live config by {edge:+.2%}"
            else:
                verdict, reason = "rejected", f"shadow: {edge:+.2%} vs live config"
        control.record_validation(sug["id"], "shadow", control.tests_tried(db),
                                  float(s["min_margin"]), verdict, reason, evidence, db)
        if done:
            control.set_status(sug["id"], verdict, reason, db)
        out.append({"id": sug["id"], "status": verdict, "note": reason})
    return out


if __name__ == "__main__":
    for r in run_shadows():
        print(f"#{r['id']}: {r['status']} ({r['note']})")
    sys.exit(0)

"""Launch Outcome Study, phase 3: outcome checkpoints and labels."""

import sqlite3
from datetime import timedelta

import httpx
import pytest

from solpaper.client import Http
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.study.outcomes import label, peak_multiple
from solpaper.study.runner import StudyRunner
from solpaper.tests.test_study_phase2 import T0, Clock, add_pair, iso, observe

LC = load_config()["study"]["labels"]


def snap(liq=None, vol=None, price=None, ratio=None, grad=0):
    return {"liquidity_usd": liq, "volume_h24": vol, "price_usd": price, "price_ratio": ratio,
            "graduated": grad}


@pytest.mark.parametrize("s,expected", [
    (snap(), "unknown"),                                              # no data is not dead
    (snap(liq=200, vol=50, price=1e-6, ratio=0.5), "dead"),
    (snap(liq=None, vol=10, price=1e-7, ratio=0.05), "dead"),         # curve coin collapsed
    (snap(liq=50_000, vol=5_000, price=1e-6, ratio=2, grad=1), "graduated"),
    (snap(liq=20_000, vol=100, price=1e-6, ratio=0.3), "survived"),
    (snap(liq=None, vol=50_000, price=1e-6, ratio=0.8), "survived"),
    (snap(liq=3_000, vol=2_000, price=1e-6, ratio=0.4), "flopped"),
])
def test_labels(s, expected):
    assert label(s, LC, "pool")[0] == expected


def ds_pair(mint, dex, liq, price, vol):
    return {"chainId": "solana", "dexId": dex, "pairAddress": f"P{dex}{mint}",
            "baseToken": {"address": mint, "symbol": "X"}, "quoteToken": {"symbol": "SOL"},
            "priceUsd": str(price), "liquidity": {"usd": liq} if liq is not None else None,
            "volume": {"h24": vol}, "txns": {}}


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "o.db")
    conn = connect(cfg["db_path"])
    clk = Clock(T0)
    state = {"pairs": {}, "down": False, "calls": 0}

    def net(req):
        if req.url.host == "api.dexscreener.com":
            state["calls"] += 1
            if state["down"]:
                return httpx.Response(503)
            mints = req.url.path.rsplit("/", 1)[-1].split(",")
            return httpx.Response(200, json=[p for m in mints for p in state["pairs"].get(m, [])])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"context": {"slot": 1}, "value": []}})

    http = Http(conn, cfg, transport=httpx.MockTransport(net), clock=clk, wall=clk, sleep=clk.sleep)
    runner = StudyRunner(cfg, conn=conn, http=http, wall=clk, seed="s")
    return {"cfg": cfg, "conn": conn, "clk": clk, "state": state, "r": runner}


def at(env, minutes):
    env["clk"].t = (T0 + timedelta(minutes=minutes)).timestamp()


def outcome(env, pair, cp):
    return env["conn"].execute("SELECT * FROM launch_outcomes WHERE pair_address = ? AND "
                               "checkpoint = ?", (pair, cp)).fetchone()


def test_checkpoints_are_queued_for_every_launch(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    env["r"].tick()
    due = dict(env["conn"].execute("SELECT window_min, due_at FROM study_queue WHERE task='outcome'"))
    assert due == {m: T0.timestamp() + m * 60 for m in (60, 360, 1440, 4320, 10080)}


def test_one_hour_outcome_is_labelled_from_the_lookup(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    observe(env["conn"], "A", T0 + timedelta(minutes=1), price=1.0)
    env["state"]["pairs"]["MA"] = [ds_pair("MA", "pumpswap", 25_000, 1.5, 9_000)]
    env["r"].tick()
    at(env, 61)
    env["r"].tick()
    o = outcome(env, "A", "1h")
    assert o["label"] == "survived" and o["liquidity_usd"] == 25_000
    assert o["price_ratio"] == pytest.approx(1.5) and o["peak_multiple"] == pytest.approx(1.5)


def test_token_the_provider_no_longer_lists_is_unknown_not_dead(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    env["r"].tick()
    at(env, 61)
    env["r"].tick()
    o = outcome(env, "A", "1h")
    assert o["label"] == "unknown"
    assert env["conn"].execute("SELECT COUNT(*) FROM study_gaps WHERE task='outcome'").fetchone()[0] == 1


def test_failed_lookups_retry_then_become_unknown(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    env["r"].tick()
    env["state"]["down"] = True
    at(env, 61)
    env["r"].tick()
    assert outcome(env, "A", "1h") is None                     # still retrying
    at(env, 61 + env["cfg"]["study"]["outcome_retry_min"] + 1)
    env["r"].http.backoffs["dexscreener"].success()
    env["r"].tick()
    assert outcome(env, "A", "1h")["label"] == "unknown"


def test_graduation_is_labelled_and_sampled(env):
    add_pair(env["conn"], "C", dex="pump-fun", mint="MC")
    observe(env["conn"], "C", T0 + timedelta(minutes=1), price=1.0)
    env["state"]["pairs"]["MC"] = [ds_pair("MC", "pumpfun", None, 2.0, 1_000),
                                   ds_pair("MC", "pumpswap", 40_000, 2.0, 30_000)]
    env["r"].tick()
    at(env, 61)
    env["r"].tick()
    assert outcome(env, "C", "1h")["label"] == "graduated"
    c = env["conn"].execute("SELECT sampled FROM launch_cohort WHERE pair_address='C'").fetchone()
    assert c["sampled"] == 1


def test_peak_multiple_ignores_prices_after_the_checkpoint(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    observe(env["conn"], "A", T0 + timedelta(minutes=1), price=1.0)
    observe(env["conn"], "A", T0 + timedelta(minutes=30), price=3.0)
    observe(env["conn"], "A", T0 + timedelta(minutes=90), price=50.0)   # after the 1h checkpoint
    peak, first = peak_multiple(env["conn"], "A", (T0 + timedelta(minutes=60)).timestamp(), 2.0)
    assert first == 1.0 and peak == pytest.approx(3.0)


def test_outcomes_are_append_only(env):
    add_pair(env["conn"], "A", dex="pumpswap", mint="MA")
    env["r"].tick()
    at(env, 61)
    env["r"].tick()
    with pytest.raises(sqlite3.DatabaseError):
        env["conn"].execute("UPDATE launch_outcomes SET label = 'survived'")

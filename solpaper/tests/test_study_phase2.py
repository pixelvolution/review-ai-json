"""Launch Outcome Study, phase 2. IDs refer to specs/launch-outcome-study/SPEC-phase2.md."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from solpaper.client import Http
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.study import curve as curve_mod
from solpaper.study import schema
from solpaper.study.cohort import iso, sample_u
from solpaper.study.features import compute
from solpaper.study.runner import StudyRunner
from solpaper.study.stats import report

FIX = Path(__file__).parent / "fixtures"
PKG = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
CURVES = json.loads((FIX / "rpc_pump_curves.json").read_text())
FRESH, DONE = CURVES["addresses"]
CURVE_BY_ADDR = dict(zip(CURVES["addresses"], CURVES["response"]["result"]["value"]))


class Clock:
    def __init__(self, t):
        self.t = t.timestamp()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "s.db")
    conn = connect(cfg["db_path"])
    clk = Clock(T0)
    calls = {"rpc": 0, "down": False}

    def net(req):
        calls["rpc"] += 1
        if calls["down"]:
            return httpx.Response(503)
        body = json.loads(req.content)
        vals = [CURVE_BY_ADDR.get(a) for a in body["params"][0]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"context": {"slot": 1}, "value": vals}})

    http = Http(conn, cfg, transport=httpx.MockTransport(net), clock=clk,
                wall=clk, sleep=clk.sleep)
    return {"cfg": cfg, "conn": conn, "clk": clk, "http": http, "calls": calls}


def runner(env, seed="s1"):
    return StudyRunner(env["cfg"], conn=env["conn"], http=env["http"], wall=env["clk"], seed=seed)


def add_pair(conn, pa, *, dex="pump-fun", mint=None, seen=T0, created_ago_min=1):
    conn.execute(
        "INSERT INTO pairs (pair_address, dex, base_mint, quote_mint, name, pair_created_at, "
        "first_seen_at, first_source) VALUES (?, ?, ?, 'So11111111111111111111111111111111111111112',"
        " ?, ?, ?, 'gt_new_pools')",
        (pa, dex, mint or f"M{pa}", f"{pa} / SOL",
         iso(seen - timedelta(minutes=created_ago_min)) if created_ago_min is not None else None,
         iso(seen)))


def observe(conn, pa, at, price=1e-6, liq=None, buys=10, sells=5, native=None):
    conn.execute(
        "INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, price_native, "
        "liquidity_usd, vol_m5, vol_h1, buys_h1, sells_h1) VALUES (?, ?, 'ds_refresh', ?, ?, ?, "
        "100, 200, ?, ?)", (pa, iso(at), price, native, liq, buys, sells))


# ------------------------------------------------------------------ schema

def test_sf01_features_are_append_only_and_cohort_is_fixed(env):
    add_pair(env["conn"], "A")
    r = runner(env)
    r.tick()
    env["clk"].t += 5 * 60
    r.tick()
    with pytest.raises(sqlite3.DatabaseError):
        env["conn"].execute("UPDATE launch_features SET price_usd = 1")
    with pytest.raises(sqlite3.DatabaseError):
        env["conn"].execute("DELETE FROM launch_features")
    with pytest.raises(sqlite3.DatabaseError):
        env["conn"].execute("UPDATE launch_cohort SET source = 'pool'")
    env["conn"].execute("UPDATE launch_cohort SET sampled = 1, sampled_reason = 'graduate'")
    with pytest.raises(sqlite3.DatabaseError):
        env["conn"].execute("UPDATE launch_cohort SET sampled = 0")


# ------------------------------------------------------------------ cohort

def test_sf02_sources(env):
    add_pair(env["conn"], "P", dex="pumpfun")          # alias -> pump-fun
    add_pair(env["conn"], "L", dex="raydium-launchlab")
    add_pair(env["conn"], "S", dex="pumpswap")
    runner(env).tick()
    src = dict(env["conn"].execute("SELECT pair_address, source FROM launch_cohort"))
    assert src == {"P": "pumpfun_curve", "L": "other_curve", "S": "pool"}


def test_sf03_launch_definition(env):
    add_pair(env["conn"], "NEW", created_ago_min=3)
    add_pair(env["conn"], "OLD", created_ago_min=600)
    add_pair(env["conn"], "UNK", created_ago_min=None)
    runner(env).tick()
    st = dict(env["conn"].execute("SELECT pair_address, status FROM launch_cohort"))
    assert st == {"NEW": "launch", "OLD": "excluded_not_new", "UNK": "excluded_no_created_at"}
    queued = {r[0] for r in env["conn"].execute("SELECT pair_address FROM study_queue")}
    assert queued == {"NEW"}


def test_sf04_sampling_is_random_recorded_and_seeded():
    us = [sample_u("seedX", f"pair{i}") for i in range(5000)]
    rate = sum(u < 0.10 for u in us) / len(us)
    assert 0.085 < rate < 0.115
    assert sample_u("seedX", "pair1") == sample_u("seedX", "pair1")
    assert sample_u("seedX", "pair1") != sample_u("seedY", "pair1")


def test_sf04_sampling_never_reads_filter_results():
    src = "".join((PKG / "study" / f).read_text() for f in
                  ("cohort.py", "runner.py", "features.py", "curve.py", "schema.py"))
    for table in ("filter_state", "filter_passes", "skips", "token_checks", "sell_quotes"):
        assert table not in src


def test_sf04_sampling_is_stored_and_graduates_are_always_sampled(env):
    add_pair(env["conn"], "C", dex="pump-fun", mint="MINT1")
    r = runner(env)
    r.tick()
    row = env["conn"].execute("SELECT * FROM launch_cohort WHERE pair_address='C'").fetchone()
    assert row["sample_rate"] == 0.10 and 0 <= row["sample_u"] < 1
    add_pair(env["conn"], "POOL", dex="pumpswap", mint="MINT1",
             seen=T0 + timedelta(minutes=2), created_ago_min=0)
    r.tick()
    c = env["conn"].execute("SELECT sampled, sampled_reason FROM launch_cohort "
                            "WHERE pair_address='C'").fetchone()
    assert c["sampled"] == 1 and c["sampled_reason"] in ("random", "graduate")
    p = env["conn"].execute("SELECT prior_curve_pair FROM launch_cohort WHERE pair_address='POOL'"
                            ).fetchone()
    assert p[0] == "C"


def test_sf05_feature_tasks_are_queued_before_each_cutoff(env):
    add_pair(env["conn"], "A")
    runner(env).tick()
    lead = env["cfg"]["study"]["lead_s"]
    due = dict(env["conn"].execute("SELECT window_min, due_at FROM study_queue"))
    assert due == {w: T0.timestamp() + w * 60 - lead for w in (5, 15, 60)}


# ------------------------------------------------------------------ no look-ahead

def test_sf06_features_never_use_data_after_the_cutoff(env):
    add_pair(env["conn"], "A", dex="pumpswap")
    observe(env["conn"], "A", T0 + timedelta(minutes=1), price=1.0, liq=1000)
    observe(env["conn"], "A", T0 + timedelta(minutes=4), price=2.0, liq=2000)
    observe(env["conn"], "A", T0 + timedelta(minutes=6), price=999.0, liq=10 ** 9)  # future
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10   # inside the 5-min window
    r.tick()
    f = env["conn"].execute("SELECT * FROM launch_features WHERE window_min = 5").fetchone()
    assert f["price_usd"] == 2.0 and f["liquidity_usd"] == 2000 and f["n_obs"] == 2
    assert f["data_cutoff"] <= iso(T0 + timedelta(minutes=5))


def test_sf06_a_cutoff_past_the_window_is_refused(env):
    add_pair(env["conn"], "A")
    runner(env).tick()
    m = env["conn"].execute("SELECT * FROM launch_cohort").fetchone()
    with pytest.raises(ValueError, match="look-ahead"):
        compute(env["conn"], env["cfg"], m, 5, T0 + timedelta(minutes=6), None, None, None)


# ------------------------------------------------------------------ features

def test_sf07_cheap_features_and_graduation(env):
    add_pair(env["conn"], "C", dex="pump-fun", mint="MINT1")
    observe(env["conn"], "C", T0 + timedelta(minutes=1), price=1.0, buys=30, sells=10)
    observe(env["conn"], "C", T0 + timedelta(minutes=10), price=1.5, buys=60, sells=20)
    add_pair(env["conn"], "POOL", dex="pumpswap", mint="MINT1",
             seen=T0 + timedelta(minutes=12), created_ago_min=0)
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=15)).timestamp() - 5
    r.tick()
    f = env["conn"].execute("SELECT * FROM launch_features WHERE pair_address='C' "
                            "AND window_min=15").fetchone()
    assert f["price_change"] == pytest.approx(0.5)
    assert f["buy_sell_ratio"] == pytest.approx(3.0)
    assert f["graduated"] == 1 and f["minutes_to_graduate"] == pytest.approx(12)
    f5 = env["conn"].execute("SELECT graduated FROM launch_features WHERE pair_address='C' "
                             "AND window_min=5").fetchone()
    assert f5["graduated"] == 0                      # the pool appeared after the 5-min cutoff


def test_sf08_curve_parse_on_recorded_accounts(env):
    prog = env["cfg"]["study"]["pump_program"]
    fresh = curve_mod.parse(FRESH, CURVE_BY_ADDR[FRESH], prog)
    done = curve_mod.parse(DONE, CURVE_BY_ADDR[DONE], prog)
    assert fresh.readable and not fresh.complete
    assert fresh.virtual_tokens == pytest.approx(1_073_000_000, rel=0.05)
    assert fresh.price_sol == pytest.approx(fresh.virtual_sol / fresh.virtual_tokens)
    assert done.readable and done.complete and done.price_sol is None
    assert not curve_mod.parse("X", {**CURVE_BY_ADDR[FRESH], "owner": "Other111"}, prog).readable


def test_sf08_curve_read_is_stored_with_progress_and_creator_left_unknown(env):
    add_pair(env["conn"], FRESH, dex="pump-fun")
    observe(env["conn"], FRESH, T0 + timedelta(minutes=1))
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    r.tick()
    f = env["conn"].execute("SELECT * FROM launch_features WHERE window_min=5").fetchone()
    assert f["curve_complete"] == 0 and f["curve_price_sol"] > 0
    miss = json.loads(f["missing"])
    assert "curve_progress" in miss and "creator_wallet" in miss


def test_sf09_late_task_is_a_gap_not_a_guess(env):
    add_pair(env["conn"], FRESH, dex="pump-fun")
    observe(env["conn"], FRESH, T0 + timedelta(minutes=1), price=1.0)
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=30)).timestamp()   # 5- and 15-min windows missed
    r.tick()
    rows = {x["window_min"]: x for x in env["conn"].execute("SELECT * FROM launch_features")}
    assert set(rows) == {5, 15}
    for w in (5, 15):
        assert rows[w]["curve_price_sol"] is None and rows[w]["price_usd"] == 1.0
        assert rows[w]["data_cutoff"] == iso(T0 + timedelta(minutes=w))
        assert "late" in json.loads(rows[w]["missing"])["curve_price_sol"]
    assert env["conn"].execute("SELECT COUNT(*) FROM study_gaps").fetchone()[0] == 2


def test_sf09_rpc_outage_is_a_gap(env):
    add_pair(env["conn"], FRESH, dex="pump-fun")
    r = runner(env)
    r.tick()
    env["calls"]["down"] = True
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    r.tick()
    f = env["conn"].execute("SELECT * FROM launch_features").fetchone()
    assert f["curve_price_sol"] is None
    assert "rpc unavailable" in json.loads(f["missing"])["curve_price_sol"]


def test_sf10_queue_survives_a_restart(env):
    add_pair(env["conn"], "A", dex="pumpswap")
    runner(env).tick()                                  # first process queues, then "dies"
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    runner(env, seed="s2").tick()                       # a new process picks the task up
    assert env["conn"].execute("SELECT COUNT(*) FROM launch_features").fetchone()[0] == 1
    assert env["conn"].execute("SELECT COUNT(*) FROM study_runs").fetchone()[0] == 2


def test_sf11_study_reads_use_the_study_budget_bucket(env):
    add_pair(env["conn"], FRESH, dex="pump-fun")
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    r.tick()
    buckets = {x[0] for x in env["conn"].execute("SELECT bucket FROM request_log")}
    assert buckets == {"study"}


def test_sf12_engine_never_imports_the_study():
    for f in PKG.rglob("*.py"):
        if "study" in f.parts or "tests" in f.parts:
            continue
        assert not re.search(r"(from|import)\s+[.\w]*study", f.read_text()), f


def test_sf13_stats_lead_with_sample_size_and_window(env):
    add_pair(env["conn"], "A", dex="pumpswap")
    runner(env).tick()
    out = report(env["conn"])
    first = out.splitlines()[0]
    assert first.startswith("STUDY SAMPLE: 1 launches") and "window:" in first
    assert "not a tradable edge" in out or "Nothing here is a tradable edge" in out


def test_curve_consistency_is_not_judged_for_non_sol_pairs(env):
    add_pair(env["conn"], FRESH, dex="pump-fun")
    env["conn"].execute("UPDATE pairs SET quote_mint = 'PRIVACYmint111' WHERE pair_address = ?",
                        (FRESH,))
    observe(env["conn"], FRESH, T0 + timedelta(minutes=1), native=1.0)
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    r.tick()
    f = env["conn"].execute("SELECT curve_consistent, missing FROM launch_features").fetchone()
    assert f["curve_consistent"] is None
    assert json.loads(f["missing"])["curve_consistent"] == "pair not priced in SOL"


def test_export_includes_study_findings(env, tmp_path):
    from solpaper.export import export
    add_pair(env["conn"], "A", dex="pumpswap")
    observe(env["conn"], "A", T0 + timedelta(minutes=1))
    r = runner(env)
    r.tick()
    env["clk"].t = (T0 + timedelta(minutes=5)).timestamp() - 10
    r.tick()
    counts = export(env["cfg"]["db_path"], tmp_path, env["cfg"], only="study")
    assert counts["study_cohort.csv"] == 1 and counts["study_features.csv"] == 1
    head = (tmp_path / "study_features.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "window_min" in head and "missing" in head and "source" in head

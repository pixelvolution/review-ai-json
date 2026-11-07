"""Phase 3: fill model, wash filter, trade records, holders, research. IDs: specs/solpaper-phase3-research/SPEC.md."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from solpaper import research
from solpaper.client import Http
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.fills import ExitRule, FillModel, Obs, cp_value_after_impact, simulate
from solpaper.providers.geckoterminal import GeckoTerminal, parse_trade
from solpaper.providers.solana_rpc import holder_concentration
from solpaper.wash import score as wash_score

FIX = Path(__file__).parent / "fixtures"
M = FillModel(delay_s=5, fee_rate=0.0025, tip_usd=0.05, honeypot_rate=0.0, honeypot_seed=7,
              max_fill_wait_s=90)
NO_EXIT = ExitRule(None, None, None, None)


def series(points):
    return [Obs(t, p, liq) for t, p, liq in points]


# ------------------------------------------------------------------ P3-08 fill model

def test_entry_fills_after_the_delay_never_before():
    obs = series([(100, 1.0, 1e6), (103, 0.5, 1e6), (106, 2.0, 1e6), (200, 2.0, 1e6)])
    r = simulate(obs, 100, 10, M, NO_EXIT, "P")
    assert r.entry_t == 106 and r.entry_price == 2.0   # the cheap 103 print was before t+5


def test_no_look_ahead_future_prices_dont_change_the_entry():
    base = [(100, 1.0, 1e6), (106, 1.0, 1e6), (300, 1.0, 1e6)]
    a = simulate(series(base), 100, 10, M, NO_EXIT, "P")
    b = simulate(series(base + [(400, 50.0, 1e6)]), 100, 10, M, ExitRule(None, None, 150, None),
                 "P")
    assert a.entry_price == b.entry_price == 1.0
    c = simulate(series([(50, 99.0, 1e6)] + base), 100, 10, M, NO_EXIT, "P")
    assert c.entry_price == 1.0                          # data before the signal is ignored


def test_constant_product_impact_and_costs():
    assert cp_value_after_impact(10, 20) == pytest.approx(5.0)   # quote side 10: half lost
    obs = series([(0, 1.0, 1e9), (5, 1.0, 1e9), (100, 1.0, 1e9)])
    r = simulate(obs, 0, 10, M, NO_EXIT, "P")
    assert r.spent == pytest.approx(10 * 1.0025 + 0.05)
    assert r.net == pytest.approx(10 * (1 - 0.0025) - 0.05 - r.spent, abs=1e-3)


def test_a_drained_pool_pays_almost_nothing():
    obs = series([(0, 1.0, 50_000), (5, 1.0, 50_000), (100, 12.0, 2.0)])   # "+1100%", $2 left
    r = simulate(obs, 0, 10, M, NO_EXIT, "P")
    assert r.price_only_net > 100            # the chart says huge win
    assert r.net < -9.0                      # the pool can't pay it: ~90%+ of the stake is gone


def test_honeypot_is_a_total_loss_and_still_pays_the_tip():
    hp = FillModel(5, 0.0025, 0.05, 1.0, 7, 90)
    obs = series([(0, 1.0, 1e9), (5, 1.0, 1e9), (100, 3.0, 1e9)])
    r = simulate(obs, 0, 10, hp, NO_EXIT, "P")
    assert r.honeypot and r.proceeds == pytest.approx(-0.05)
    assert r.net == pytest.approx(-r.spent - 0.05)


def test_fill_gap_is_reported_not_guessed():
    obs = series([(0, 1.0, 1e6), (500, 1.0, 1e6)])           # next print 495 s after due
    assert simulate(obs, 0, 10, M, NO_EXIT, "P").status == "no_entry_fill"


@pytest.mark.parametrize("rule,path,reason", [
    (ExitRule(0.5, None, None, None), [(50, 1.6, 1e6), (56, 1.7, 1e6)], "take_profit"),
    (ExitRule(None, 0.2, None, None), [(50, 0.7, 1e6), (56, 0.6, 1e6)], "stop_loss"),
    (ExitRule(None, None, 60, None), [(50, 1.0, 1e6), (70, 1.0, 1e6), (76, 1.0, 1e6)], "time_stop"),
    (ExitRule(None, None, None, 0.5), [(50, 1.0, 1e5), (56, 1.0, 1e5)], "liquidity_collapse"),
])
def test_exit_triggers_fill_after_the_delay(rule, path, reason):
    obs = series([(0, 1.0, 1e6), (5, 1.0, 1e6)] + path)
    r = simulate(obs, 0, 10, M, rule, "P")
    assert r.exit_reason == reason
    trig = next(t for t, *_ in path if t >= 50)
    assert r.exit_t >= trig + M.delay_s


# ------------------------------------------------------------------ P3-03/04 wash

def organic(n=40, end=10_000):
    rng = random.Random(3)
    t = end
    out = []
    for k in range(n):
        t -= rng.uniform(5, 200)
        out.append({"t": t, "wallet": f"W{k}", "kind": rng.choice(["buy", "sell"]),
                    "volume_usd": rng.uniform(5, 800)})
    return out


def washy(n=40, end=10_000):
    out = []
    for k in range(n):        # 3 wallets ping-ponging identical sizes every 30 s
        out.append({"t": end - k * 30, "wallet": f"B{k % 3}", "kind": "buy" if k % 2 else "sell",
                    "volume_usd": 100.0})
    return out


def test_wash_score_separates_organic_from_wash():
    cfg = load_config()["wash"]
    o, w = wash_score(organic(), 10_000, cfg), wash_score(washy(), 10_000, cfg)
    assert o["status"] == w["status"] == "ok"
    assert not o["flagged"] and w["flagged"] and w["score"] > o["score"]
    assert "funding_cluster" in w["unavailable"]


def test_wash_ignores_trades_after_as_of():
    cfg = load_config()["wash"]
    early = organic(end=5_000)
    late = washy(end=9_000)                              # wash trading starts later
    r = wash_score(early + late, 5_000, cfg)
    assert r["metrics"]["n_trades"] == 40 and not r["flagged"]


def test_too_few_trades_is_never_a_pass():
    cfg = load_config()["wash"]
    r = wash_score(organic(n=5), 10_000, cfg)
    assert r["status"] == "insufficient_trades" and r["flagged"] is None


# ------------------------------------------------------------------ P3-01 trades, P3-05 holders

def test_parse_recorded_trade():
    d = json.loads((FIX / "gt_trades.json").read_text())
    item = d["response"]["data"][0]
    a = item["attributes"]
    base = a["to_token_address"] if a["kind"] == "buy" else a["from_token_address"]
    t = parse_trade(item, d["pool"], base)
    assert t.trade_id == item["id"] and t.wallet == a["tx_from_address"]
    side = "to" if a["kind"] == "buy" else "from"
    assert t.price_usd == float(a[f"price_{side}_in_usd"])
    assert t.volume_usd == pytest.approx(float(a["volume_in_usd"]))


def test_trades_429_is_none(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "t.db")
    conn = connect(cfg["db_path"])
    http = Http(conn, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    assert GeckoTerminal(http, cfg).trades("POOL", "MINT") is None


def test_holder_concentration_excludes_the_pairs_own_account():
    largest = [{"address": "curveATA", "amount": "600"}, {"address": "w1", "amount": "100"},
               {"address": "w2", "amount": "50"}]
    h = holder_concentration(largest, 1000, {"curveATA": "PAIR", "w1": "x", "w2": "y"}, "PAIR")
    assert h["top_share"] == pytest.approx(0.75) and h["top_share_ex_pair"] == pytest.approx(0.15)


# ------------------------------------------------------------------ P3-06 engine wash stage

@pytest.fixture
def eng(tmp_path):
    from solpaper.tests.test_phase2 import NOW, Clock, Net, add_pair, clean_trades  # noqa: F401
    from solpaper.filters import Filters
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "e.db")
    conn = connect(cfg["db_path"])
    net, clk = Net(), Clock()
    clk.t = NOW.timestamp()
    feeds = {}

    def handler(req):
        if req.url.host == "api.geckoterminal.com":
            pool = req.url.path.split("/")[-2]
            mode = feeds.get(pool, "clean")
            if mode == "down":
                return httpx.Response(503)
            if mode == "wash":
                end = NOW.timestamp()
                data = [{"id": f"x{pool}{k}", "type": "trade", "attributes": {
                    "block_timestamp": datetime.fromtimestamp(end - k * 30, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"), "tx_from_address": f"B{k % 3}",
                    "kind": "buy" if k % 2 else "sell", "volume_in_usd": "100",
                    "to_token_address": "X", "from_token_address": "Y"}} for k in range(40)]
                return httpx.Response(200, json={"data": data})
            if mode == "few":
                return httpx.Response(200, json=clean_trades(n=5, pool=pool))
        return net(req)

    http = Http(conn, cfg, transport=httpx.MockTransport(handler), clock=clk, wall=lambda: clk.t,
                sleep=clk.sleep)
    f = Filters(conn, http, cfg, wall=lambda: clk.t)
    return {"conn": conn, "f": f, "feeds": feeds, "add": add_pair, "now": NOW, "clk": clk}


def test_engine_wash_flags_and_skips(eng):
    eng["add"](eng["conn"], "W")
    eng["feeds"]["W"] = "wash"
    d = eng["f"].evaluate(["W"], eng["now"])[0]
    assert d.reason == "wash_score_high" and "round_trip" in d.detail


def test_engine_wash_insufficient_trades(eng):
    eng["add"](eng["conn"], "F")
    eng["feeds"]["F"] = "few"
    assert eng["f"].evaluate(["F"], eng["now"])[0].reason == "wash_insufficient_trades"


def test_engine_wash_outage_is_unavailable_never_a_pass(eng):
    eng["add"](eng["conn"], "D")
    eng["feeds"]["D"] = "down"
    d = eng["f"].evaluate(["D"], eng["now"])[0]
    assert (d.outcome, d.reason) == ("skip", "check_unavailable:wash")


def test_engine_wash_is_budgeted_per_pass(eng):
    for p in "ABC":
        eng["add"](eng["conn"], p)
    d = eng["f"].evaluate(list("ABC"), eng["now"])
    reasons = sorted(x.reason or "pass" for x in d)
    assert reasons.count("wash_check_pending") == 2


# ------------------------------------------------------------------ P3-07 research

def test_research_sample_is_seeded_and_recorded(eng):
    pop = [{"pair_address": f"P{i}"} for i in range(50)]
    a = research.draw_sample(pop, 10, "seed1")
    b = research.draw_sample(pop, 10, "seed1")
    c = research.draw_sample(pop, 10, "seed2")
    assert [p["pair_address"] for _, p in a] == [p["pair_address"] for _, p in b]
    assert [p["pair_address"] for _, p in a] != [p["pair_address"] for _, p in c]


def test_research_entry_signal_uses_only_the_past(eng):
    eng["add"](eng["conn"], "A", liq=4_000, age_min=30, reading_age_s=600)   # not yet eligible
    eng["add"](eng["conn"], "A", liq=60_000, reading_age_s=300)               # eligible here
    p = eng["conn"].execute("SELECT * FROM pairs WHERE pair_address='A'").fetchone()
    t_sig, row = research.entry_signal(eng["conn"], load_config(), p)
    assert row["liquidity_usd"] == 60_000
    assert t_sig == pytest.approx((eng["now"] - timedelta(seconds=300)).timestamp())


def test_research_small_groups_are_insufficient_data_and_hypotheses_are_recorded(eng):
    cfg = load_config()
    conn = eng["conn"]
    results = [{"pair": f"P{i}", "rule": "tp20_sl10_t15m", "status": "closed", "net": 1.0,
                "price_only_net": 1.0, "wash_status": "ok", "wash_flagged": False,
                "activity": "<100 trades/24h", "concentration": "concentration unknown"}
               for i in range(5)]
    sample = [(0.1, {"first_seen_at": "2026-11-07T20:00:00+00:00"})]
    out = research.summarize(conn, cfg, 1, results, sample)
    assert out.startswith("RESEARCH SAMPLE: 1 pool launches")
    assert "insufficient data / likely noise" in out
    assert conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] >= 1


def test_binomial_tail():
    assert research.binom_tail(0, 10) == pytest.approx(1.0)
    assert research.binom_tail(10, 10) == pytest.approx(1 / 1024)

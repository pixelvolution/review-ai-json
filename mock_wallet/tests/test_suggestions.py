"""Suggestion + validation pipeline tests, on synthetic price history."""

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import control
import live_config
import market
import replay
import validate_suggestion as vs
from set_config import set_param
from shadow import run_shadows
from wallet import WalletConfig

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
A = ("2025-02-01", "2025-03-01")
B = ("2025-03-01", "2025-04-01")
CHEAP = WalletConfig(max_drawdown=1, fee_rate="0.001", slippage_rate="0.0005", tip=0)


def synth(pairs, start="2025-01-01", end="2025-04-01", seed=0):
    rng = np.random.default_rng(seed)
    t = pd.date_range(start, end, freq="1h", tz="UTC", inclusive="left")
    out = {}
    for k, p in enumerate(pairs):
        r = rng.normal(0, 0.02, len(t))
        close = 1.0 * np.exp(np.cumsum(r))
        openp = np.r_[1.0, close[:-1]]
        hi = np.maximum(openp, close) * (1 + rng.uniform(0, 0.01, len(t)))
        lo = np.minimum(openp, close) * (1 - rng.uniform(0, 0.01, len(t)))
        out[p] = pd.DataFrame({"t": t, "open": openp, "high": hi, "low": lo, "close": close,
                               "volume": rng.uniform(1e5, 1e6, len(t))})
    return out


def seed_market(path, data):
    for p, df in data.items():
        market.record_bars(df, p, path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    mdb = tmp_path / "market.db"
    seed_market(mdb, synth(PAIRS + ["BTC"]))
    cfg_path = tmp_path / "live_config.json"
    live_config.write({"strategy": "random_entry", "params": {"prob": 0.03, "hold": 12, "seed": 1},
                       "universe": PAIRS, "regime_pair": "BTC"}, cfg_path)
    settings = {"research_window": list(A), "holdout_window": list(B), "warmup_hours": 200,
                "min_trades": 50, "min_margin": 0.02, "noise_sigma": 0.02,
                "max_dd_tolerance": 0.0, "fill_delay_bars": 1, "honeypot_rate": 0.02,
                "honeypot_seed": 7, "shadow": {"enabled": False, "days": 14, "min_trades": 20}}
    set_path = tmp_path / "validation.json"
    set_path.write_text(json.dumps(settings))
    cdb = tmp_path / "control.db"
    monkeypatch.setenv("MOCK_WALLET_MARKET_DB", str(mdb))
    monkeypatch.setenv("MOCK_WALLET_LIVE_CONFIG", str(cfg_path))
    monkeypatch.setenv("MOCK_WALLET_VALIDATION", str(set_path))
    monkeypatch.setenv("MOCK_WALLET_CONTROL_DB", str(cdb))
    return {"market": mdb, "config": cfg_path, "settings": settings, "control": cdb,
            "tmp": tmp_path}


def validate(env, sid, **kw):
    return vs.validate(sid, settings=kw.pop("settings", env["settings"]), wallet_cfg=CHEAP, **kw)


# ------------------------------------------------------------------ MCP tool

def test_suggest_change_writes_pending_and_never_touches_config(env):
    import mcp_server
    before = env["config"].read_bytes()
    r = mcp_server.suggest_change("hold", 24, "longer holds rode trends in window A")
    assert r["ok"] and r["status"] == "pending"
    assert r["derived_window"] == list(A)
    assert env["config"].read_bytes() == before
    rows = control.list_suggestions()
    assert [(x["parameter"], x["proposed_value"], x["status"]) for x in rows] == [
        ("params.hold", 24, "pending")]

    bad = mcp_server.suggest_change("max_drawdown", 0.9, "loosen the kill switch")
    assert not bad["ok"] and "not tunable" in bad["error"]
    assert len(control.list_suggestions()) == 1
    assert env["config"].read_bytes() == before


def test_suggest_change_through_the_mcp_protocol(env):
    import mcp_server
    tools = asyncio.run(mcp_server.server.list_tools())
    assert [t.name for t in tools] == ["suggest_change"]
    res = asyncio.run(mcp_server.server.call_tool(
        "suggest_change", {"parameter": "params.prob", "proposed_value": 0.02,
                           "rationale": "more trades"}))
    assert not res.is_error
    assert json.loads(res.content[0].text)["status"] == "pending"


# ------------------------------------------------------------------ replay honesty

def test_replay_uses_no_future_data(env, tmp_path):
    cfg = live_config.load()
    s = env["settings"]
    clean = replay.run_replay(cfg, A, s, CHEAP)

    # Same history up to the window end, garbage afterwards: identical result.
    data = synth(PAIRS + ["BTC"])
    end = pd.Timestamp(A[1], tz="UTC")
    rng = np.random.default_rng(99)
    for df in data.values():
        after = df.t >= end
        for c in ("open", "high", "low", "close"):
            df.loc[after, c] = rng.uniform(0.01, 100, after.sum())
    other = tmp_path / "poisoned.db"
    seed_market(other, data)
    poisoned = replay.run_replay(cfg, A, s, CHEAP, market_db=other)
    assert poisoned.trade_log == clean.trade_log
    assert poisoned.ret == clean.ret

    # The loader never returns a bar at or after the window end.
    bars = market.load_bars(PAIRS, "2025-01-01", A[1])
    assert all(df.index.max() < end for df in bars.values())


def test_replay_decisions_only_use_the_past(env):
    """Cutting the window short can't change any entry made before the cut."""
    cfg = live_config.load()
    s = env["settings"]
    full = replay.run_replay(cfg, A, s, CHEAP)
    cut = "2025-02-15"
    short = replay.run_replay(cfg, (A[0], cut), s, CHEAP)
    key = lambda log: [(x["symbol"], x["entry_t"], x["entry_px"], x["honeypot"]) for x in log]
    cutoff = pd.Timestamp(cut, tz="UTC")
    early_full = sorted(k for k in key(full.trade_log) if k[1] < cutoff)
    assert sorted(key(short.trade_log)) == early_full
    assert len(early_full) > 10


def test_fill_delay_and_honeypots(env):
    import engine
    t = pd.date_range("2025-01-01", periods=10, freq="1h", tz="UTC")
    px = pd.DataFrame({"X": np.linspace(1, 2, 10)}, index=t)
    panel = engine.Panel(t, px, px * 1.001, px * 0.999, px, px * 0 + 1e6)
    entry = np.zeros((10, 1), bool)
    entry[2, 0] = True
    spec = engine.Spec(entry=entry, max_hold=2)
    r = engine.backtest(panel, spec, CHEAP, 0, 10, fill_delay=3)
    assert r.trade_log[0]["entry_t"] == t[5]  # signal at bar 2's close, fills 3 bars later
    with pytest.raises(ValueError):
        engine.backtest(panel, spec, CHEAP, 0, 10, fill_delay=0)

    # Honeypots depend only on (seed, pair, entry time): a sell is a total loss.
    hp = engine.backtest(panel, spec, CHEAP, 0, 10, fill_delay=3, honeypot_rate=1.0)
    assert hp.honeypots == 1 and hp.trade_log[0]["exit_px"] == 0.0 and hp.avg_trade == -1
    assert engine.is_honeypot(7, "X", t[5], 0.3) == engine.is_honeypot(7, "X", t[5], 0.3)


# ------------------------------------------------------------------ judging

def W(base_ret, cand_ret, trades=100, base_dd=0.2, cand_dd=0.2):
    return {"baseline": {"trades": trades, "ret": base_ret, "max_dd": base_dd},
            "candidate": {"trades": trades, "ret": cand_ret, "max_dd": cand_dd}}


def test_one_window_win_is_likely_noise(env):
    s = env["settings"]
    verdict, reason, _ = vs.judge({"replay": W(0.0, 0.20), "holdout": W(0.0, -0.05)}, s, 1)
    assert verdict == "likely noise" and "replay" in reason
    verdict, _, _ = vs.judge({"replay": W(0.0, -0.05), "holdout": W(0.0, 0.20)}, s, 1)
    assert verdict == "likely noise"
    # Winning on return but with a worse drawdown is not a win.
    verdict, _, _ = vs.judge({"replay": W(0.0, 0.20), "holdout": W(0.0, 0.20, cand_dd=0.3)}, s, 1)
    assert verdict == "likely noise"
    assert vs.judge({"replay": W(0, 0.2), "holdout": W(0, 0.2)}, s, 1)[0] == "validated"


def test_one_window_winner_is_rejected_end_to_end(env, monkeypatch):
    sid = control.add_suggestion("hold", 24, "x")["id"]
    fake = iter([W(0.0, 0.30), W(0.0, -0.10)])
    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: next(fake))
    r = validate(env, sid)
    assert r["status"] == "likely noise"
    assert live_config.load()["params"]["hold"] == 12


def test_insufficient_data_never_passes(env):
    s = env["settings"]
    huge = {"replay": W(0.0, 5.0, trades=49), "holdout": W(0.0, 5.0)}
    assert vs.judge(huge, s, 1)[0] == "insufficient data"
    assert vs.judge({"replay": W(0, 5.0), "holdout": None}, s, 1)[0] == "insufficient data"

    # A real replay on a one-day window can't reach 50 trades.
    tiny = dict(s, research_window=["2025-02-01", "2025-02-02"])
    sid = control.add_suggestion("prob", 0.05, "x", "2025-02-01", "2025-02-02")["id"]
    assert validate(env, sid, settings=tiny)["status"] == "insufficient data"

    # A holdout with no stored data at all.
    nodata = dict(s, holdout_window=["2030-01-01", "2030-02-01"])
    sid = control.add_suggestion("prob", 0.06, "x")["id"]
    assert validate(env, sid, settings=nodata)["status"] == "insufficient data"


def test_real_replay_produces_evidence(env):
    sid = control.add_suggestion("hold", 24, "longer holds")["id"]
    r = validate(env, sid)
    assert r["status"] in ("validated", "likely noise", "rejected")
    v = control.latest_validation(sid)
    for name in ("replay", "holdout"):
        w = v["evidence"]["windows"][name]
        assert w["baseline"]["trades"] >= 50 and w["candidate"]["trades"] >= 50
    assert v["tests_tried"] == 1


def test_margin_rises_with_tests_tried(env, monkeypatch):
    s = env["settings"]
    margins = [vs.required_margin(n, s) for n in (1, 2, 10, 100, 1000)]
    assert margins == sorted(margins) and margins[0] == s["min_margin"]
    # A +3% edge passes as the first test but not after many tries.
    assert vs.judge({"replay": W(0, 0.03), "holdout": W(0, 0.03)}, s, 1)[0] == "validated"
    assert vs.judge({"replay": W(0, 0.03), "holdout": W(0, 0.03)}, s, 100)[0] == "rejected"

    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: W(0, -0.1))
    for v in (13, 14, 15):
        validate(env, control.add_suggestion("hold", v, "x")["id"])
    assert control.tests_tried() == 3
    assert [control.latest_validation(i)["tests_tried"] for i in (1, 2, 3)] == [1, 2, 3]


def test_validated_never_auto_applies(env, monkeypatch):
    before = env["config"].read_bytes()
    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: W(0.0, 0.5))
    sid = control.add_suggestion("hold", 24, "x")["id"]
    assert validate(env, sid)["status"] == "validated"
    assert env["config"].read_bytes() == before


def test_holdout_must_not_overlap_the_derived_window(env):
    sid = control.add_suggestion("hold", 24, "x", "2025-02-15", "2025-03-15")["id"]
    with pytest.raises(ValueError, match="overlaps"):
        validate(env, sid)
    assert control.get_suggestion(sid)["status"] == "pending"
    assert control.tests_tried() == 0


# ------------------------------------------------------------------ isolation

ENGINE_MODULES = ["wallet.py", "trader.py", "sizer.py", "market.py", "replay.py",
                  "live_config.py", "research/engine.py", "research/strategies.py",
                  "research/data.py"]


def test_engine_source_never_mentions_suggestions():
    for m in ENGINE_MODULES:
        src = (ROOT / m).read_text().lower()
        assert not re.search(r"suggest|control", src), f"{m} references suggestions/control"


def test_engine_never_opens_the_control_db(env, monkeypatch):
    cfg = live_config.load()
    s = env["settings"]
    clean = replay.run_replay(cfg, A, s, CHEAP)
    for v in (24, 48, 96):  # a pile of pending suggestions
        control.add_suggestion("hold", v, "x")
    opened = []
    real = sqlite3.connect

    def spy(path, *a, **k):
        opened.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    again = replay.run_replay(cfg, A, s, CHEAP)
    assert str(env["control"]) not in opened
    assert again.trade_log == clean.trade_log and again.ret == clean.ret


# ------------------------------------------------------------------ config changes

def test_config_changes_are_logged(env, monkeypatch):
    set_param("hold", 24, "testing")
    log = control.config_changes()
    assert log[0]["source"] == "set_config" and log[0]["parameter"] == "params.hold"
    assert json.loads(log[0]["old_value"]) == 12 and json.loads(log[0]["new_value"]) == 24
    with pytest.raises(ValueError):
        set_param("hold", 36, "")  # a reason is required

    cfg = live_config.load()  # a hand edit
    cfg["params"]["prob"] = 0.05
    live_config.write(cfg)
    assert control.check_unlogged_edits() == ["params.prob"]
    assert control.config_changes()[0]["source"] == "manual edit detected"
    assert control.check_unlogged_edits() == []

    with pytest.raises(sqlite3.DatabaseError):
        control.connect().execute("DELETE FROM config_changes")


def test_applying_a_suggestion_needs_validation_or_force(env, monkeypatch):
    sid = control.add_suggestion("hold", 24, "x")["id"]
    with pytest.raises(ValueError, match="not validated"):
        set_param(None, None, "go", suggestion_id=sid)
    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: W(0.0, 0.5))
    validate(env, sid)
    set_param(None, None, "evidence looks good", suggestion_id=sid)
    assert live_config.load()["params"]["hold"] == 24
    assert control.get_suggestion(sid)["status"] == "applied"
    assert control.config_changes()[0]["suggestion_id"] == sid


# ------------------------------------------------------------------ shadow

def test_shadow_mode_gates_promotion(env, monkeypatch):
    s = dict(env["settings"], shadow={"enabled": True, "days": 14, "min_trades": 20})
    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: W(0.0, 0.5))
    sid = control.add_suggestion("hold", 24, "x")["id"]
    assert validate(env, sid, settings=s)["status"] == "shadow"
    control.set_status(sid, "shadow", "test", shadow_started_at=B[0] + "T00:00:00+00:00")

    mid = run_shadows(settings=s, now=pd.Timestamp("2025-03-05", tz="UTC"), wallet_cfg=CHEAP)
    assert mid[0]["status"] == "shadow"  # period not over
    done = run_shadows(settings=s, now=pd.Timestamp(B[1], tz="UTC"), wallet_cfg=CHEAP)
    assert done[0]["status"] in ("validated", "rejected", "insufficient data")
    assert control.get_suggestion(sid)["status"] == done[0]["status"]
    assert live_config.load()["params"]["hold"] == 12


# ------------------------------------------------------------------ reporting

def test_report_and_dashboard_show_validated_evidence(env, monkeypatch):
    from dashboard import render_suggestions
    from report import suggestion_board, suggestions_markdown
    monkeypatch.setattr(vs, "_run_pair", lambda *a, **k: {
        **W(0.01, 0.2, trades=80), "window": ["2025-02-01T00:00:00+00:00", "2025-03-01"]})
    sid = control.add_suggestion("hold", 24, "longer holds ride trends")["id"]
    validate(env, sid)
    board = suggestion_board()
    assert board["tests_tried"] == 1 and board["items"][0]["id"] == sid
    md = "\n".join(suggestions_markdown(board))
    for needle in ("Tests tried so far: **1**", "awaiting your approval", "80/80", "+19.00%",
                   "set_config.py --suggestion"):
        assert needle in md
    html = render_suggestions(board)
    assert "Tests tried" in html and "longer holds ride trends" in html

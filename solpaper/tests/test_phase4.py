"""Phase 4: mock wallet + paper executor + risk."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dec

import pytest

from solpaper.config import load_config
from solpaper.db import connect
from solpaper.executor import Executor
from solpaper.fills import cp_value_after_impact
from solpaper.reset import reset
from solpaper.wallet import InsufficientFunds, MockWallet, RunCooked, WalletConfig

T0 = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "p4.db")
    cfg["fills"]["honeypot_rate"] = 0.0
    conn = connect(cfg["db_path"])
    clk = Clock()

    def make(**over):
        c = {**cfg, "fills": {**cfg["fills"], **over.pop("fills", {})},
             "trading": {**cfg["trading"], **over.pop("trading", {})},
             "wallet": {**cfg["wallet"], **over.pop("wallet", {})}}
        wallet = MockWallet(cfg["db_path"], WalletConfig(**c["wallet"]))
        return Executor(conn, c, wall=clk, wallet=wallet)

    return {"cfg": cfg, "conn": conn, "clk": clk, "make": make}


def add_pair(conn, pa, passed=True):
    conn.execute("INSERT INTO pairs (pair_address, dex, name, first_seen_at, first_source) "
                 "VALUES (?, 'pumpswap', ?, ?, 'test')", (pa, f"{pa} / SOL", iso(T0)))
    conn.execute("INSERT INTO filter_state (pair_address, outcome, reason, updated_at) "
                 "VALUES (?, ?, ?, ?)", (pa, "pass" if passed else "skip",
                                         None if passed else "liquidity_below_floor", iso(T0)))


def obs(conn, pa, dt, price, liq=50_000.0):
    conn.execute("INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, "
                 "liquidity_usd) VALUES (?, ?, 'ds_refresh', ?, ?)", (pa, iso(T0 + dt), price, liq))


def at(env, dt):
    env["clk"].t = T0 + dt


def trade(env, pa="A"):
    return env["conn"].execute("SELECT * FROM trades WHERE pair_address = ?", (pa,)).fetchone()


# ------------------------------------------------------------------ fills: delay, no look-ahead

def test_entry_waits_for_the_delay_and_fills_at_the_first_later_observation(env):
    add_pair(env["conn"], "A")
    obs(env["conn"], "A", -10, 1.0)
    ex = env["make"]()
    ex.tick()                                       # signal at T0
    assert trade(env)["status"] == "pending_entry"
    obs(env["conn"], "A", 2, 0.5)                   # before T0 + delay: must be ignored
    obs(env["conn"], "A", 30, 2.0)
    at(env, 40)
    ex.tick()
    t = trade(env)
    assert t["status"] == "open" and t["entry_mid"] == 2.0 and t["entry_delay_s"] == 30


def test_entry_price_includes_constant_product_impact_and_costs_are_debited(env):
    add_pair(env["conn"], "A")
    ex = env["make"]()
    ex.tick()
    obs(env["conn"], "A", 10, 1.0, liq=1_000)       # thin pool: quote side $500
    at(env, 15)
    ex.tick()
    t = trade(env)
    got = cp_value_after_impact(10, 1_000)
    assert t["notional"] == 10 and t["entry_price"] == pytest.approx(10 / got)
    assert t["entry_impact"] == pytest.approx(1 - got / 10)
    assert ex.wallet.balance == Dec("100") - Dec("10") - Dec("0.025") - Dec("0.05")


def test_take_profit_exit_fills_after_the_delay_and_the_trade_is_logged(env):
    add_pair(env["conn"], "A")
    ex = env["make"]()
    ex.tick()
    obs(env["conn"], "A", 10, 1.0)
    at(env, 15)
    ex.tick()
    obs(env["conn"], "A", 60, 1.6)                  # +60% >= take profit 50%
    at(env, 61)
    ex.tick()
    assert trade(env)["status"] == "pending_exit"
    obs(env["conn"], "A", 63, 9.0)                  # before trigger + delay: ignored
    obs(env["conn"], "A", 90, 1.55)
    at(env, 95)
    ex.tick()
    t = trade(env)
    assert t["status"] == "closed" and t["exit_reason"] == "take_profit"
    assert t["exit_mid"] == 1.55 and t["exit_delay_s"] == 30
    assert t["pnl"] > 0 and t["fees"] > 0 and t["tips"] == pytest.approx(0.10)
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM signals ORDER BY id")]
    assert kinds == ["entry", "exit"]


def test_liquidity_collapse_forces_an_exit(env):
    add_pair(env["conn"], "A")
    ex = env["make"]()
    ex.tick()
    obs(env["conn"], "A", 10, 1.0, liq=50_000)
    at(env, 15)
    ex.tick()
    obs(env["conn"], "A", 40, 1.0, liq=10_000)
    at(env, 41)
    ex.tick()
    assert trade(env)["exit_reason"] == "liquidity_collapse"


def test_honeypot_is_a_failed_sell_and_a_total_loss(env):
    add_pair(env["conn"], "A")
    ex = env["make"](fills={"honeypot_rate": 1.0})
    ex.tick()
    obs(env["conn"], "A", 10, 1.0)
    at(env, 15)
    ex.tick()
    obs(env["conn"], "A", 60, 2.0)
    at(env, 61)
    ex.tick()
    obs(env["conn"], "A", 70, 2.0)
    at(env, 75)
    ex.tick()
    t = trade(env)
    assert t["status"] == "failed_sell"
    assert t["proceeds"] == pytest.approx(-0.05)       # nothing back; the tip still paid
    assert t["pnl"] == pytest.approx(-(10 + 0.025 + 0.05) - 0.05)


def test_entry_with_no_observation_in_time_is_a_gap_not_a_fill(env):
    add_pair(env["conn"], "A")
    ex = env["make"]()
    ex.tick()
    at(env, 500)
    ex.tick()
    assert trade(env)["status"] == "entry_failed"
    assert ex.wallet.balance == Dec("100")


# ------------------------------------------------------------------ sizer + risk

def test_never_trades_a_pair_that_did_not_pass_the_filters(env):
    add_pair(env["conn"], "S", passed=False)
    env["make"]().tick()
    assert env["conn"].execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_one_trade_per_pair_and_max_open_positions(env):
    for p in "ABC":
        add_pair(env["conn"], p)
    ex = env["make"](trading={"max_open_positions": 2})
    ex.tick()
    ex.tick()
    assert env["conn"].execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
    skip = env["conn"].execute("SELECT stage, reason FROM skips").fetchone()
    assert tuple(skip) == ("sizer", "max_open_positions")


def test_daily_loss_cap_blocks_new_entries(env):
    add_pair(env["conn"], "A")
    env["conn"].execute("INSERT INTO trades (pair_address, status, pnl, exit_fill_t) "
                        "VALUES ('OLD', 'closed', -30, ?)", (T0,))
    env["make"]().tick()
    assert env["conn"].execute("SELECT COUNT(*) FROM trades WHERE pair_address = 'A'"
                               ).fetchone()[0] == 0
    assert env["conn"].execute("SELECT reason FROM skips").fetchone()[0] == "daily_loss_cap"


def test_position_size_follows_equity(env):
    ex = env["make"](wallet={"position_fraction": 0.2})
    assert ex.size() == Dec("20")


def test_kill_switch_cooks_the_run_and_halts_trading(env):
    add_pair(env["conn"], "A")
    ex = env["make"](wallet={"position_fraction": 0.9},
                     trading={"exit": {"take_profit": None, "stop_loss": None,
                                       "time_stop_min": 600, "liquidity_collapse": None}})
    ex.tick()
    obs(env["conn"], "A", 10, 1.0)
    at(env, 15)
    ex.tick()
    obs(env["conn"], "A", 40, 0.5)                 # -50% on 90% of equity: > 25% drawdown
    at(env, 41)
    ex.tick()
    assert ex.wallet.is_cooked and ex.halted
    add_pair(env["conn"], "B")
    ex.tick()
    assert env["conn"].execute("SELECT COUNT(*) FROM trades WHERE pair_address = 'B'"
                               ).fetchone()[0] == 0
    assert env["conn"].execute("SELECT COUNT(*) FROM events WHERE kind = 'trading_halted'"
                               ).fetchone()[0] == 1


# ------------------------------------------------------------------ wallet (ported rules)

def test_wallet_cannot_overdraw(env):
    w = MockWallet(env["cfg"]["db_path"], WalletConfig(**env["cfg"]["wallet"]))
    with pytest.raises(InsufficientFunds):
        w.open_position("X", 1, 150)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE wallet_runs SET balance = -1")


def test_vault_skim_is_never_traded(env):
    w = MockWallet(env["cfg"]["db_path"], WalletConfig(**{**env["cfg"]["wallet"],
                                                          "position_fraction": 0.5}))
    p = w.open_position("X", 1, 50)
    w.close_position(p["position_id"], 5)           # equity way past 2x start
    assert w.vault > 0 and w.balance == Dec("100")
    with pytest.raises(InsufficientFunds):
        w.open_position("Y", 1, 150)               # balance + vault would cover it; balance can't
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE wallet_runs SET vault = 0")


def test_cooked_run_is_frozen_and_reset_starts_the_next(env):
    w = MockWallet(env["cfg"]["db_path"], WalletConfig(**env["cfg"]["wallet"]))
    p = w.open_position("X", 1, 60)
    w.close_position(p["position_id"], 0.5)
    assert w.is_cooked
    with pytest.raises(RunCooked):
        w.open_position("Y", 1, 10)
    with pytest.raises(sqlite3.DatabaseError):
        w.conn.execute("UPDATE wallet_runs SET status = 'alive'")
    assert reset(env["cfg"]["db_path"], WalletConfig(**env["cfg"]["wallet"])) == 2


def test_exit_into_a_drained_pool_closes_at_zero_instead_of_hanging(env):
    add_pair(env["conn"], "A")
    ex = env["make"]()
    ex.tick()
    obs(env["conn"], "A", 10, 1.0, liq=50_000)
    at(env, 15)
    ex.tick()
    obs(env["conn"], "A", 40, 1.0, liq=0.0)        # rug: liquidity collapse trigger
    at(env, 41)
    ex.tick()
    obs(env["conn"], "A", 50, 1.0, liq=0.0)
    at(env, 55)
    ex.tick()
    t = trade(env)
    assert t["status"] == "closed" and t["exit_reason"] == "liquidity_collapse"
    assert t["pnl"] == pytest.approx(-(10 + 0.025 + 0.05) - 0.05, abs=1e-3)

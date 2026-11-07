import sqlite3
from decimal import Decimal as Dec

import pytest

from report import morning_report, runs_history, wallet_status
from dashboard import render_dashboard
from report import recent_events
from reset import reset
from sizer import size_position
from trader import PaperTrader
from wallet import (InsufficientFunds, MockWallet, RunCooked, TradeTooSmall,
                    WalletConfig, WalletError, from_micros)

# No slippage so the arithmetic in assertions is exact; slippage has its own test.
EXACT = dict(fee_rate="0.01", slippage_rate="0", tip="0.05")


@pytest.fixture
def db(tmp_path):
    return tmp_path / "wallet.db"


def make(db, **kw):
    return MockWallet(db, WalletConfig(**{**EXACT, **kw}))


def ledger_sum(w):
    row = w.conn.execute("SELECT SUM(amount) s FROM ledger WHERE run_id = ?",
                         (w.run_id,)).fetchone()
    return from_micros(row["s"])


def snapshots(w):
    return w.conn.execute("SELECT * FROM snapshots WHERE run_id = ? ORDER BY snapshot_id",
                          (w.run_id,)).fetchall()


# --- run 1 -----------------------------------------------------------------

def test_first_run_is_run_1_with_default_100(db):
    w = MockWallet(db)
    assert w.run_id == 1
    assert w.balance == Dec("100") == w.start_balance
    assert w.status == "alive"
    assert w.vault == 0


def test_reopening_keeps_the_runs_original_config(db):
    make(db, start_balance=50).close()
    w = MockWallet(db, WalletConfig(start_balance=500))
    assert w.run_id == 1 and w.balance == Dec("50")


# --- overdraw --------------------------------------------------------------

def test_wallet_cannot_overdraw(db):
    w = make(db)
    with pytest.raises(InsufficientFunds):
        w.open_position("X", 1, 101)
    # Notional fits but notional + fee + tip doesn't.
    with pytest.raises(InsufficientFunds):
        w.open_position("X", 1, 99.95)
    assert w.balance == Dec("100")
    assert w.open_positions() == []

    # The largest affordable trade fits exactly, and leaves nothing to overdraw with.
    n = w.max_affordable_notional()
    w.open_position("X", 1, n)
    assert 0 <= w.balance < Dec("0.01")
    with pytest.raises((InsufficientFunds, TradeTooSmall)):
        w.open_position("Y", 1, 5)


def test_database_rejects_negative_balance(db):
    w = make(db)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE runs SET balance = -1 WHERE run_id = 1")


def test_sizer_never_exceeds_available_balance(db):
    w = make(db, position_fraction="1")
    size = size_position(w)
    assert w.entry_cost(size) <= w.balance


# --- fees ------------------------------------------------------------------

def test_fees_and_tips_reduce_balance_exactly(db):
    w = make(db)
    pos = w.open_position("X", 1, 10)
    # 100 - 10 notional - 0.10 fee - 0.05 tip
    assert w.balance == Dec("89.85")
    assert pos["cost_basis"] == Dec("10.15")

    closed = w.close_position(pos["position_id"], 1)
    # + 10 gross - 0.10 fee - 0.05 tip
    assert w.balance == Dec("99.70")
    assert closed["proceeds"] == Dec("9.85")
    assert closed["pnl"] == Dec("-0.30")
    assert ledger_sum(w) == w.balance


def test_slippage_costs_tokens_like_a_real_swap(db):
    w = make(db, slippage_rate="0.01")
    pos = w.open_position("X", 1, 10)
    assert Dec(pos["qty"]) < 10  # paid 1.01 per token
    assert pos["entry_slippage"] > 0
    closed = w.close_position(pos["position_id"], 1)  # flat price, still loses
    assert closed["exit_slippage"] > 0
    assert w.balance < Dec("99.70")  # worse than the no-slippage round trip
    assert ledger_sum(w) == w.balance


# --- failed sell -----------------------------------------------------------

def test_failed_sell_zeroes_the_position(db):
    w = make(db)
    pos = w.open_position("RUG", 1, 20)
    before = w.balance
    w.mark({"RUG": 3})
    assert w.open_position_value() > 0

    res = w.close_position(pos["position_id"], failed=True)
    assert res["status"] == "failed"
    assert res["proceeds"] == Dec("-0.05")  # nothing back, and the tip still burned
    assert res["pnl"] == -(pos["cost_basis"] + Dec("0.05"))
    assert w.open_position_value() == 0
    assert w.balance == before - Dec("0.05")
    kinds = [r["kind"] for r in w.conn.execute("SELECT kind FROM events")]
    assert "failed_sell" in kinds


def test_failed_sell_without_tip_charge(db):
    w = make(db, charge_tip_on_failed_sell=False)
    pos = w.open_position("RUG", 1, 20)
    before = w.balance
    res = w.close_position(pos["position_id"], failed=True)
    assert res["proceeds"] == 0 and w.balance == before


# --- sizing compounds --------------------------------------------------------

def test_size_follows_equity(db):
    t = PaperTrader(make(db, position_fraction="0.2"))
    first = size_position(t.wallet)
    assert first == Dec("20")
    pos = t.buy("WIN", 1)
    t.sell(pos["position_id"], 1.5)
    after_win = size_position(t.wallet)
    assert after_win > first

    pos = t.buy("LOSE", 1)
    t.sell(pos["position_id"], 0.6)
    assert size_position(t.wallet) < after_win


# --- snapshots -------------------------------------------------------------

def test_snapshot_after_every_open_and_close(db):
    w = make(db)
    assert [s["trigger"] for s in snapshots(w)] == ["start"]
    pos = w.open_position("X", 1, 10)
    w.close_position(pos["position_id"], 1.2)
    snaps = snapshots(w)
    assert [s["trigger"] for s in snaps] == ["start", "open", "close"]
    last = snaps[-1]
    assert from_micros(last["balance"]) == w.balance
    assert from_micros(last["vault"]) == w.vault
    assert from_micros(last["peak_equity"]) == w.peak_equity
    assert last["open_position_value"] == 0
    assert from_micros(snaps[1]["open_position_value"]) == Dec("10")


# --- cooked ----------------------------------------------------------------

def test_drawdown_kill_switch_cooks_and_halts_trading(db):
    t = PaperTrader(make(db))
    w = t.wallet
    pos = w.open_position("X", 1, 50)
    with pytest.raises(RunCooked) as exc:
        t.sell(pos["position_id"], 0.45)  # ~28% drawdown
    assert "drawdown" in exc.value.cause
    assert w.is_cooked and w.status == "cooked"
    assert "drawdown" in w.cooked_reason

    with pytest.raises(RunCooked):
        t.buy("Y", 1)
    with pytest.raises(RunCooked):
        w.open_position("Y", 1, 10)
    with pytest.raises(RunCooked):
        w.mark({"Y": 1})


def test_equity_below_min_trade_size_cooks(db):
    w = make(db, max_drawdown="1", min_trade_size="5")
    pos = w.open_position("RUG", 1, 96)
    w.close_position(pos["position_id"], failed=True)
    assert w.is_cooked
    assert "minimum viable trade size" in w.cooked_reason


def test_mark_can_cook_and_liquidates_open_positions(db):
    w = make(db)
    a = w.open_position("A", 1, 40)
    b = w.open_position("B", 1, 40)
    w.mark({"A": 0.3})
    assert w.is_cooked
    assert w.open_positions() == []
    for p in (a, b):
        assert w.get_position(p["position_id"])["close_reason"] == "kill switch liquidation"
    assert ledger_sum(w) == w.balance


def test_cooked_run_is_never_auto_restarted_or_edited(db):
    w = make(db)
    pos = w.open_position("X", 1, 60)
    w.close_position(pos["position_id"], 0.5)
    assert w.is_cooked
    frozen = dict(w._run())
    w.close()

    w = MockWallet(db)  # reopening does not start a new run
    assert w.run_id == 1 and w.is_cooked
    for sql in ("UPDATE runs SET status = 'alive' WHERE run_id = 1",
                "UPDATE runs SET balance = 100 WHERE run_id = 1",
                "DELETE FROM runs WHERE run_id = 1",
                "DELETE FROM ledger",
                "UPDATE snapshots SET balance = 0",
                "DELETE FROM events",
                "DELETE FROM positions",
                "INSERT INTO events (run_id, ts, kind, message) VALUES (1, 'x', 'x', 'x')"):
        with pytest.raises(sqlite3.DatabaseError):
            w.conn.execute(sql)
    assert dict(w._run()) == frozen


def test_reset_creates_next_run_and_keeps_history(db):
    w = make(db)
    pos = w.open_position("X", 1, 60)
    w.close_position(pos["position_id"], 0.5)
    run1 = dict(w._run())
    w.close()

    assert reset(db, WalletConfig(start_balance=250)) == 2
    w2 = MockWallet(db)
    assert w2.run_id == 2 and w2.status == "alive" and w2.balance == Dec("250")
    hist = runs_history(db)
    assert [h["run_id"] for h in hist] == [1, 2]
    assert [h["status"] for h in hist] == ["cooked", "alive"]
    assert dict(w2.conn.execute("SELECT * FROM runs WHERE run_id = 1").fetchone()) == run1


def test_reset_refuses_while_alive_unless_forced(db):
    make(db).open_position("X", 1, 10)
    with pytest.raises(WalletError):
        reset(db)
    assert reset(db, force=True) == 2
    hist = runs_history(db)
    assert hist[0]["status"] == "cooked"
    assert "manual reset" in hist[0]["cooked_reason"]


# --- cup rule / vault ------------------------------------------------------

def test_vault_skim_is_logged_and_never_traded(db):
    t = PaperTrader(make(db, position_fraction="0.5"))
    w = t.wallet
    pos = w.open_position("MOON", 1, 50)
    w.close_position(pos["position_id"], 4)  # 50 -> ~200 gross, equity ~ 247
    assert w.vault > 0
    assert w.balance == Dec("100")  # excess above start capital moved out
    assert w.equity() == Dec("100")
    assert not w.is_cooked  # the skim is a transfer, not a drawdown
    ev = w.conn.execute("SELECT * FROM events WHERE kind = 'vault_skim'").fetchall()
    assert len(ev) == 1
    vault = w.vault

    # The sizer only sees tradable equity.
    assert w.entry_cost(size_position(w)) <= w.balance
    # Balance + vault would cover this, the balance alone doesn't.
    with pytest.raises(InsufficientFunds):
        w.open_position("X", 1, 120)

    # Losses come out of the balance, never the vault, even when the run dies.
    pos = w.open_position("DUMP", 1, 60)
    w.close_position(pos["position_id"], failed=True)
    assert w.is_cooked
    assert w.vault == vault

    # And the database refuses to shrink it.
    w2 = make(db.with_name("other.db"))
    w2.conn.execute("UPDATE runs SET vault = 10 WHERE run_id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        w2.conn.execute("UPDATE runs SET vault = 5 WHERE run_id = 1")


def test_no_skim_at_or_below_target(db):
    w = make(db)
    pos = w.open_position("X", 1, 20)
    w.close_position(pos["position_id"], 2)
    assert w.equity() < Dec("200") and w.vault == 0


# --- reporting -------------------------------------------------------------

def test_max_drawdown_tracks_marks_between_snapshots(db):
    w = make(db)
    w.open_position("X", 1, 50)
    w.mark({"X": 0.7})  # ~15% down, no snapshot written
    w.mark({"X": 1.0})
    hist = runs_history(db)[0]
    assert hist["max_drawdown"] > Dec("0.14")
    assert not w.is_cooked


def test_status_report_and_dashboard_show_required_fields(db):
    w = make(db)
    pos = w.open_position("X", 1, 50)
    w.close_position(pos["position_id"], 0.45)
    s = wallet_status(w)
    for k in ("start_balance", "balance", "vault", "peak_equity", "drawdown",
              "status", "run_id"):
        assert k in s
    w.close()
    reset(db)

    text = morning_report(db)
    assert "Run #2 (ALIVE)" in text and "| Vault |" in text and "Current drawdown" in text
    assert "## All runs" in text and "| #1 | cooked |" in text

    w = MockWallet(db)
    html = render_dashboard(wallet_status(w), runs_history(db), recent_events(db))
    for needle in ("Run", "ALIVE", "Start balance", "Current balance", "Vault",
                   "Peak equity", "Current drawdown", "Cause of death"):
        assert needle in html

"""MockWallet: a SQLite-backed paper-money ledger with real consequences.

The wallet is a ledger only. It holds no keys, no addresses and makes no
network calls. Every paper trade debits the wallet on entry and credits it on
exit, and every fee, slippage, tip and failed-sell loss costs exactly what it
would cost real money.

Money is kept as integer micro-dollars (1e-6 USD) in the database and as
``Decimal`` in Python, so there is no float drift. Rounding is always against
us: charges round up and proceeds round down.

Rules enforced here (and, where possible, by the database itself):

* The balance can never go negative (CHECK constraint).
* A trade can never be larger than the available balance, costs included.
* COOKED: if drawdown from peak equity reaches ``max_drawdown`` or equity
  falls below ``min_trade_size``, the run is liquidated, marked ``cooked`` and
  frozen. Triggers reject any later write to a cooked run, and nothing can
  ever be deleted. Only ``python reset.py`` starts a new run.
* CUP RULE: when equity exceeds ``vault_target_multiple`` x start capital, the
  excess above start capital moves into the vault. The vault can only grow,
  and nothing in this module can spend it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Iterator

log = logging.getLogger("mock_wallet")

MICRO = Decimal("0.000001")
QTY_STEP = Decimal("1e-18")
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "mock_wallet.db"


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------

def D(value) -> Decimal:
    """Convert int/str/float/Decimal to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):  # includes numpy.float64, whose repr isn't a number
        return Decimal(repr(float(value)))
    return Decimal(str(value))


def q_down(value) -> Decimal:
    """Round to the micro-dollar, toward zero money for us (proceeds)."""
    return D(value).quantize(MICRO, rounding=ROUND_FLOOR)


def q_up(value) -> Decimal:
    """Round to the micro-dollar, against us (charges)."""
    return D(value).quantize(MICRO, rounding=ROUND_CEILING)


def to_micros(value: Decimal) -> int:
    return int(q_down(value) * 1_000_000)


def from_micros(micros: int) -> Decimal:
    return (Decimal(micros) / 1_000_000).quantize(MICRO)


def usd(value) -> str:
    """Human-readable dollars for log/event messages (data fields stay exact)."""
    return f"${D(value):,.2f}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class WalletError(Exception):
    """Base class for every wallet refusal."""


class RunCooked(WalletError):
    """The run is cooked. Trading is halted until ``python reset.py``."""

    def __init__(self, run_id: int, cause: str, result: dict | None = None):
        self.run_id = run_id
        self.cause = cause
        self.result = result  # the operation that tipped it over, if any
        super().__init__(f"run {run_id} is COOKED: {cause}. "
                         f"Run `python reset.py` to start a new run.")


class InsufficientFunds(WalletError):
    """The trade (including costs) is larger than the available balance."""


class TradeTooSmall(WalletError):
    """The trade is below the configured minimum viable trade size."""


class PositionNotFound(WalletError):
    """No such open position in the current run."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WalletConfig:
    """Rules for one run. Stored on the run row so every life keeps its rules."""

    start_balance: Decimal = Decimal("100")
    min_trade_size: Decimal = Decimal("5")
    max_drawdown: Decimal = Decimal("0.25")
    vault_target_multiple: Decimal = Decimal("2")
    position_fraction: Decimal = Decimal("0.10")
    fee_rate: Decimal = Decimal("0.01")
    slippage_rate: Decimal = Decimal("0.01")
    tip: Decimal = Decimal("0.05")
    charge_tip_on_failed_sell: bool = True

    def __post_init__(self):
        for f in fields(self):
            if f.name != "charge_tip_on_failed_sell":
                object.__setattr__(self, f.name, D(getattr(self, f.name)))
        if self.start_balance <= 0:
            raise ValueError("start_balance must be > 0")
        if self.min_trade_size <= 0:
            raise ValueError("min_trade_size must be > 0")
        if self.start_balance < self.min_trade_size:
            raise ValueError("start_balance must be >= min_trade_size")
        if not (0 < self.max_drawdown <= 1):
            raise ValueError("max_drawdown must be in (0, 1]")
        if self.vault_target_multiple <= 1:
            raise ValueError("vault_target_multiple must be > 1")
        if not (0 < self.position_fraction <= 1):
            raise ValueError("position_fraction must be in (0, 1]")
        for name in ("fee_rate", "slippage_rate"):
            if not (0 <= getattr(self, name) < 1):
                raise ValueError(f"{name} must be in [0, 1)")
        if self.tip < 0:
            raise ValueError("tip must be >= 0")

    @property
    def vault_target(self) -> Decimal:
        return q_down(self.start_balance * self.vault_target_multiple)

    def to_json(self) -> str:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        return json.dumps({k: (str(v) if isinstance(v, Decimal) else v)
                           for k, v in data.items()}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "WalletConfig":
        data = json.loads(text)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_env(cls, **overrides) -> "WalletConfig":
        """Defaults <- MOCK_WALLET_<FIELD> env vars <- explicit overrides."""
        kwargs = {}
        for f in fields(cls):
            raw = os.environ.get(f"MOCK_WALLET_{f.name.upper()}")
            if raw is not None:
                if f.name == "charge_tip_on_failed_sell":
                    kwargs[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
                else:
                    kwargs[f.name] = raw
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_APPEND_ONLY = ("ledger", "events", "snapshots")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY,
    started_at    TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('alive', 'cooked')),
    config        TEXT    NOT NULL,
    start_balance INTEGER NOT NULL CHECK (start_balance > 0),
    balance       INTEGER NOT NULL CHECK (balance >= 0),
    vault         INTEGER NOT NULL DEFAULT 0 CHECK (vault >= 0),
    peak_equity   INTEGER NOT NULL CHECK (peak_equity >= 0),
    max_drawdown  TEXT    NOT NULL DEFAULT '0',
    cooked_at     TEXT,
    cooked_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_alive ON runs(status) WHERE status = 'alive';

CREATE TABLE IF NOT EXISTS positions (
    position_id    INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES runs(run_id),
    symbol         TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('open', 'closed', 'failed')),
    opened_at      TEXT    NOT NULL,
    closed_at      TEXT,
    quote_entry    TEXT    NOT NULL,
    fill_entry     TEXT    NOT NULL,
    qty            TEXT    NOT NULL,
    notional       INTEGER NOT NULL,
    entry_fee      INTEGER NOT NULL,
    entry_tip      INTEGER NOT NULL,
    entry_slippage INTEGER NOT NULL,
    cost_basis     INTEGER NOT NULL,
    mark_price     TEXT    NOT NULL,
    quote_exit     TEXT,
    fill_exit      TEXT,
    gross_exit     INTEGER,
    exit_fee       INTEGER,
    exit_tip       INTEGER,
    exit_slippage  INTEGER,
    proceeds       INTEGER,
    pnl            INTEGER,
    close_reason   TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id      INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    ts            TEXT    NOT NULL,
    position_id   INTEGER REFERENCES positions(position_id),
    kind          TEXT    NOT NULL,
    amount        INTEGER NOT NULL,
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    note          TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    run_id   INTEGER NOT NULL REFERENCES runs(run_id),
    ts       TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    message  TEXT    NOT NULL,
    data     TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id         INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    ts                  TEXT    NOT NULL,
    trigger             TEXT    NOT NULL,
    position_id         INTEGER,
    balance             INTEGER NOT NULL,
    vault               INTEGER NOT NULL,
    open_position_value INTEGER NOT NULL,
    equity              INTEGER NOT NULL,
    peak_equity         INTEGER NOT NULL,
    drawdown            TEXT    NOT NULL
);

-- Runs are permanent history.
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'runs are permanent history and can never be deleted'); END;

CREATE TRIGGER IF NOT EXISTS runs_cooked_frozen BEFORE UPDATE ON runs
WHEN OLD.status = 'cooked'
BEGIN SELECT RAISE(ABORT, 'cooked runs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS runs_vault_only_grows BEFORE UPDATE OF vault ON runs
WHEN NEW.vault < OLD.vault
BEGIN SELECT RAISE(ABORT, 'the vault is never withdrawn or traded'); END;

-- Positions: never deleted, frozen once closed, frozen with a cooked run.
CREATE TRIGGER IF NOT EXISTS positions_no_delete BEFORE DELETE ON positions
BEGIN SELECT RAISE(ABORT, 'positions are permanent history'); END;

CREATE TRIGGER IF NOT EXISTS positions_insert_cooked BEFORE INSERT ON positions
WHEN (SELECT status FROM runs WHERE run_id = NEW.run_id) IS NOT 'alive'
BEGIN SELECT RAISE(ABORT, 'cannot trade on a run that is not alive'); END;

CREATE TRIGGER IF NOT EXISTS positions_closed_frozen BEFORE UPDATE ON positions
WHEN OLD.status <> 'open'
  OR (SELECT status FROM runs WHERE run_id = OLD.run_id) IS NOT 'alive'
BEGIN SELECT RAISE(ABORT, 'closed positions and cooked runs are immutable'); END;
""" + "".join(f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_insert_cooked BEFORE INSERT ON {t}
WHEN (SELECT status FROM runs WHERE run_id = NEW.run_id) IS NOT 'alive'
BEGIN SELECT RAISE(ABORT, 'cooked runs are immutable'); END;
""" for t in _APPEND_ONLY)


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(db_path or os.environ.get("MOCK_WALLET_DB") or DEFAULT_DB_PATH)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    if "max_drawdown" not in cols:  # databases created before the column existed
        conn.execute("ALTER TABLE runs ADD COLUMN max_drawdown TEXT NOT NULL DEFAULT '0'")
    return conn


# --------------------------------------------------------------------------
# The wallet
# --------------------------------------------------------------------------

class MockWallet:
    """Ledger for the latest run in the database.

    On an empty database, run 1 is created with ``config`` (or the env/default
    config). On an existing database ``config`` is ignored: the rules a run was
    started with stay its rules. The wallet never creates a new run on its
    own; a cooked run stays cooked until ``reset.py`` is run.
    """

    def __init__(self, db_path=None, config: WalletConfig | None = None,
                 create: bool = True):
        self.conn = connect(db_path)
        row = self.conn.execute("SELECT MAX(run_id) AS r FROM runs").fetchone()
        if row["r"] is None:
            if not create:
                raise WalletError("no runs in database yet")
            with self._tx():
                self._create_run_locked(config or WalletConfig.from_env())
            row = self.conn.execute("SELECT MAX(run_id) AS r FROM runs").fetchone()
        self.run_id: int = row["r"]
        self.config = WalletConfig.from_json(self._run()["config"])

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- transactions ---------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---- reads ------------------------------------------------------------

    def _run(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?",
                                 (self.run_id,)).fetchone()

    @property
    def status(self) -> str:
        return self._run()["status"]

    @property
    def is_cooked(self) -> bool:
        return self.status == "cooked"

    @property
    def cooked_reason(self) -> str | None:
        return self._run()["cooked_reason"]

    @property
    def start_balance(self) -> Decimal:
        return from_micros(self._run()["start_balance"])

    @property
    def balance(self) -> Decimal:
        """Tradable cash. Excludes the vault."""
        return from_micros(self._run()["balance"])

    @property
    def vault(self) -> Decimal:
        return from_micros(self._run()["vault"])

    @property
    def peak_equity(self) -> Decimal:
        return from_micros(self._run()["peak_equity"])

    def open_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE run_id = ? AND status = 'open' "
            "ORDER BY position_id", (self.run_id,)).fetchall()
        return [self._position_dict(r) for r in rows]

    def get_position(self, position_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE position_id = ? AND run_id = ?",
            (position_id, self.run_id)).fetchone()
        if row is None:
            raise PositionNotFound(f"position {position_id} not in run {self.run_id}")
        return self._position_dict(row)

    def open_position_value(self) -> Decimal:
        """Mark-to-market value of open positions, before exit costs."""
        rows = self.conn.execute(
            "SELECT qty, mark_price FROM positions WHERE run_id = ? AND status = 'open'",
            (self.run_id,)).fetchall()
        return q_down(sum((D(r["qty"]) * D(r["mark_price"]) for r in rows), Decimal(0)))

    def equity(self) -> Decimal:
        """Tradable equity: cash + open positions. The vault is not equity."""
        return self.balance + self.open_position_value()

    def drawdown(self) -> Decimal:
        peak, eq = self.peak_equity, self.equity()
        if peak <= 0 or eq >= peak:
            return Decimal(0)
        return (peak - eq) / peak

    def entry_cost(self, notional) -> Decimal:
        """Total debit for a buy of ``notional``: notional + fee + tip."""
        notional = q_down(notional)
        return notional + q_up(notional * self.config.fee_rate) + q_up(self.config.tip)

    def max_affordable_notional(self) -> Decimal:
        """Largest notional whose full entry cost fits in the balance."""
        cfg = self.config
        room = self.balance - q_up(cfg.tip)
        if room <= 0:
            return Decimal(0)
        n = q_down(room / (1 + cfg.fee_rate))
        while n > 0 and self.entry_cost(n) > self.balance:
            n -= MICRO
        return max(n, Decimal(0))

    # ---- trading ----------------------------------------------------------

    def open_position(self, symbol: str, quote_price, notional) -> dict:
        """Buy ``notional`` dollars of ``symbol`` at ``quote_price``.

        Debits notional + fee + tip. Slippage is taken in the fill price, so
        it costs tokens, exactly like a real swap. Refuses rather than
        shrinking the order if the wallet can't afford it.
        """
        cfg = self.config
        quote = D(quote_price)
        notional = q_down(notional)
        if quote <= 0:
            raise ValueError("quote_price must be > 0")
        with self._tx():
            self._require_alive()
            if notional < cfg.min_trade_size:
                raise TradeTooSmall(
                    f"trade ${notional} is below min viable trade size ${cfg.min_trade_size}")
            fee = q_up(notional * cfg.fee_rate)
            tip = q_up(cfg.tip)
            total = notional + fee + tip
            balance = self.balance
            if total > balance:
                raise InsufficientFunds(
                    f"trade needs ${total} (notional ${notional} + fee ${fee} + tip ${tip}) "
                    f"but only ${balance} is available")

            fill = quote * (1 + cfg.slippage_rate)
            qty = (notional / fill).quantize(QTY_STEP, rounding=ROUND_FLOOR)
            slippage = q_up(notional - qty * quote)
            ts = now_iso()
            cur = self.conn.execute(
                """INSERT INTO positions (run_id, symbol, status, opened_at, quote_entry,
                       fill_entry, qty, notional, entry_fee, entry_tip, entry_slippage,
                       cost_basis, mark_price)
                   VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.run_id, symbol, ts, str(quote), str(fill), str(qty),
                 to_micros(notional), to_micros(fee), to_micros(tip),
                 to_micros(slippage), to_micros(total), str(quote)))
            pid = cur.lastrowid
            self._ledger_locked("buy", -notional, pid, f"buy {qty} {symbol} @ {fill}")
            self._ledger_locked("fee", -fee, pid, "entry fee")
            self._ledger_locked("tip", -tip, pid, "entry tip")
            self._event_locked("open", f"opened #{pid} {symbol} for {usd(total)}",
                               {"position_id": pid, "symbol": symbol, "notional": str(notional),
                                "fee": str(fee), "tip": str(tip), "slippage": str(slippage)})
            self._snapshot_locked("open", pid)
            self._check_kill_locked()
        return self.get_position(pid)

    def close_position(self, position_id: int, quote_price=None, failed: bool = False,
                       reason: str = "exit") -> dict:
        """Sell an open position.

        ``failed=True`` is a failed sell: the position is a total loss, nothing
        is credited, and the attempt's tip is still burned (configurable).
        """
        if not failed and (quote_price is None or D(quote_price) < 0):
            raise ValueError("quote_price is required (and >= 0) for a normal sell")
        with self._tx():
            self._require_alive()
            pos = self._open_row_locked(position_id)
            self._close_locked(pos, quote_price, failed, reason)
            self._skim_locked()
            self._snapshot_locked("close", position_id)
            self._check_kill_locked()
        return self.get_position(position_id)

    def mark(self, prices: dict) -> None:
        """Update mark prices. ``prices`` maps position_id or symbol -> price.

        Peak equity tracks marks, and the kill switch is checked, so an open
        position bleeding out can cook the run.
        """
        with self._tx():
            self._require_alive()
            for pos in self.conn.execute(
                    "SELECT position_id, symbol FROM positions "
                    "WHERE run_id = ? AND status = 'open'", (self.run_id,)).fetchall():
                price = prices.get(pos["position_id"], prices.get(pos["symbol"]))
                if price is None:
                    continue
                if D(price) < 0:
                    raise ValueError("mark price must be >= 0")
                self.conn.execute("UPDATE positions SET mark_price = ? WHERE position_id = ?",
                                  (str(D(price)), pos["position_id"]))
            self._raise_peak_locked()
            self._check_kill_locked()

    def cook(self, cause: str) -> None:
        """Manually cook the current run (used by ``reset.py --force``)."""
        with self._tx():
            self._require_alive()
            self._cook_locked(cause)

    # ---- internals (call only inside _tx) -------------------------------

    def _require_alive(self):
        run = self._run()
        if run["status"] != "alive":
            raise RunCooked(self.run_id, run["cooked_reason"] or "cooked")

    def _open_row_locked(self, position_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE position_id = ? AND run_id = ? AND status = 'open'",
            (position_id, self.run_id)).fetchone()
        if row is None:
            raise PositionNotFound(f"no open position {position_id} in run {self.run_id}")
        return row

    def _close_locked(self, pos: sqlite3.Row, quote_price, failed: bool, reason: str):
        cfg = self.config
        pid = pos["position_id"]
        qty = D(pos["qty"])
        tip = q_up(cfg.tip)
        if failed:
            quote = D(quote_price) if quote_price is not None else None
            fill = None
            gross = fee = slippage = Decimal(0)
            if not cfg.charge_tip_on_failed_sell:
                tip = Decimal(0)
            status = "failed"
        else:
            quote = D(quote_price)
            fill = quote * (1 - cfg.slippage_rate)
            gross = q_down(qty * fill)
            fee = q_up(gross * cfg.fee_rate)
            slippage = q_up(qty * quote - gross) if quote > 0 else Decimal(0)
            status = "closed"

        self._ledger_locked("sell", gross, pid, f"sell {qty} {pos['symbol']} @ {fill}")
        fee = self._charge_locked("fee", fee, pid, "exit fee")
        tip = self._charge_locked("tip", tip, pid,
                                  "tip burned on failed sell" if failed else "exit tip")
        proceeds = gross - fee - tip
        pnl = proceeds - from_micros(pos["cost_basis"])
        self.conn.execute(
            """UPDATE positions SET status = ?, closed_at = ?, quote_exit = ?, fill_exit = ?,
                   gross_exit = ?, exit_fee = ?, exit_tip = ?, exit_slippage = ?,
                   proceeds = ?, pnl = ?, close_reason = ?,
                   mark_price = CASE WHEN ? = 'failed' THEN '0' ELSE mark_price END
               WHERE position_id = ?""",
            (status, now_iso(), None if quote is None else str(quote),
             None if fill is None else str(fill), to_micros(gross), to_micros(fee),
             to_micros(tip), to_micros(slippage), int(proceeds * 1_000_000),
             int(pnl * 1_000_000), reason, status, pid))
        if failed:
            self._event_locked("failed_sell",
                               f"sell of #{pid} {pos['symbol']} FAILED: position zeroed, "
                               f"loss {usd(-pnl)}",
                               {"position_id": pid, "pnl": str(pnl), "tip": str(tip)})
        else:
            self._event_locked("close", f"closed #{pid} {pos['symbol']} pnl {usd(pnl)}",
                               {"position_id": pid, "gross": str(gross), "fee": str(fee),
                                "tip": str(tip), "slippage": str(slippage),
                                "pnl": str(pnl), "reason": reason})

    def _charge_locked(self, kind: str, amount: Decimal, pid, note: str) -> Decimal:
        """Debit up to ``amount``; never below zero. Returns what was charged."""
        charged = min(amount, self.balance)
        if charged < amount:
            note += f" (wanted ${amount}, wallet only had ${charged})"
        self._ledger_locked(kind, -charged, pid, note)
        return charged

    def _ledger_locked(self, kind: str, amount: Decimal, pid=None, note=None):
        if amount == 0:
            return
        new_balance = self._run()["balance"] + int(amount * 1_000_000)
        if new_balance < 0:
            raise InsufficientFunds(f"{kind} of ${amount} would overdraw the wallet")
        self.conn.execute("UPDATE runs SET balance = ? WHERE run_id = ?",
                          (new_balance, self.run_id))
        self.conn.execute(
            "INSERT INTO ledger (run_id, ts, position_id, kind, amount, balance_after, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, now_iso(), pid, kind, int(amount * 1_000_000), new_balance, note))

    def _event_locked(self, kind: str, message: str, data: dict | None = None):
        self.conn.execute(
            "INSERT INTO events (run_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, now_iso(), kind, message, json.dumps(data) if data else None))

    def _raise_peak_locked(self):
        eq = self.equity()
        if eq > self.peak_equity:
            self.conn.execute("UPDATE runs SET peak_equity = ? WHERE run_id = ?",
                              (to_micros(eq), self.run_id))

    def _snapshot_locked(self, trigger: str, pid=None):
        self._raise_peak_locked()
        open_value = self.open_position_value()
        balance = self.balance
        self.conn.execute(
            """INSERT INTO snapshots (run_id, ts, trigger, position_id, balance, vault,
                   open_position_value, equity, peak_equity, drawdown)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, now_iso(), trigger, pid, to_micros(balance), to_micros(self.vault),
             to_micros(open_value), to_micros(balance + open_value),
             to_micros(self.peak_equity), str(self.drawdown().quantize(Decimal("0.000001")))))

    def _skim_locked(self):
        """CUP RULE: above the high-water target, bank the excess over start."""
        cfg = self.config
        equity = self.equity()
        if equity <= cfg.vault_target:
            return
        skim = q_down(min(equity - cfg.start_balance, self.balance))
        if skim <= 0:
            return
        self._ledger_locked("vault_skim", -skim, None, "cup rule: moved to vault")
        run = self._run()
        # Moving money to the vault is a transfer, not a loss: lower the peak
        # by the same amount so the skim can't trip the drawdown kill switch.
        new_peak = max(run["peak_equity"] - to_micros(skim), 0)
        self.conn.execute("UPDATE runs SET vault = vault + ?, peak_equity = ? WHERE run_id = ?",
                          (to_micros(skim), new_peak, self.run_id))
        msg = (f"cup rule: equity {usd(equity)} > target {usd(cfg.vault_target)}; "
               f"skimmed {usd(skim)} to vault (vault now {usd(self.vault)})")
        log.info(msg)
        self._event_locked("vault_skim", msg, {"amount": str(skim), "equity_before": str(equity),
                                               "vault_after": str(self.vault)})

    def _check_kill_locked(self):
        cfg = self.config
        eq, dd = self.equity(), self.drawdown()
        if dd > D(self._run()["max_drawdown"]):
            self.conn.execute("UPDATE runs SET max_drawdown = ? WHERE run_id = ?",
                              (str(dd.quantize(MICRO)), self.run_id))
        if dd >= cfg.max_drawdown:
            cause = (f"drawdown kill switch: equity {usd(eq)} is {dd:.2%} below peak "
                     f"{usd(self.peak_equity)} (limit {cfg.max_drawdown:.0%})")
        elif eq < cfg.min_trade_size:
            cause = (f"equity {usd(eq)} fell below the minimum viable trade size "
                     f"{usd(cfg.min_trade_size)}")
        else:
            return
        self._cook_locked(cause)

    def _cook_locked(self, cause: str):
        log.critical("RUN %s COOKED: %s", self.run_id, cause)
        self._event_locked("kill", f"cooking run {self.run_id}: {cause}")
        # Flatten at the last mark, paying full exit costs, so the cooked
        # record is final. No new positions can open from here.
        for pos in self.conn.execute(
                "SELECT * FROM positions WHERE run_id = ? AND status = 'open'",
                (self.run_id,)).fetchall():
            self._close_locked(pos, D(pos["mark_price"]), failed=False,
                               reason="kill switch liquidation")
            self._snapshot_locked("close", pos["position_id"])
        self._event_locked("cooked", f"run {self.run_id} COOKED: {cause}",
                           {"balance": str(self.balance), "vault": str(self.vault)})
        self._snapshot_locked("cooked")
        self.conn.execute(
            "UPDATE runs SET status = 'cooked', cooked_at = ?, cooked_reason = ? WHERE run_id = ?",
            (now_iso(), cause, self.run_id))

    def _create_run_locked(self, cfg: WalletConfig) -> int:
        start = to_micros(cfg.start_balance)
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status, config, start_balance, balance, vault, "
            "peak_equity) VALUES (?, 'alive', ?, ?, 0, 0, ?)",
            (now_iso(), cfg.to_json(), start, start))
        self.run_id = cur.lastrowid
        self.config = cfg
        self._ledger_locked("deposit", cfg.start_balance, None, "starting balance")
        self._event_locked("run_started",
                           f"run {self.run_id} started with {usd(cfg.start_balance)}",
                           json.loads(cfg.to_json()))
        self._snapshot_locked("start")
        log.info("run %s started with $%s", self.run_id, cfg.start_balance)
        return self.run_id

    @staticmethod
    def _position_dict(row: sqlite3.Row) -> dict:
        money = {"notional", "entry_fee", "entry_tip", "entry_slippage", "cost_basis",
                 "gross_exit", "exit_fee", "exit_tip", "exit_slippage", "proceeds", "pnl"}
        return {k: (from_micros(row[k]) if k in money and row[k] is not None else row[k])
                for k in row.keys()}


def start_new_run(db_path=None, config: WalletConfig | None = None) -> int:
    """Create run N+1. Refuses while a run is alive. Used by ``reset.py``."""
    w = MockWallet.__new__(MockWallet)  # bypass __init__: it would load/create a run
    w.conn = connect(db_path)
    try:
        with w._tx():
            alive = w.conn.execute(
                "SELECT run_id FROM runs WHERE status = 'alive'").fetchone()
            if alive is not None:
                raise WalletError(
                    f"run {alive['run_id']} is still alive; it must be cooked first")
            return w._create_run_locked(config or WalletConfig.from_env())
    finally:
        w.close()

"""Paper executor: sizer -> signals -> honest fills -> mock wallet -> risk.

Runs as the scanner's ``trade`` job. Plain rules, no LLM anywhere in the loop.

  * Entry: a pair whose current filter decision is ``pass`` (it cleared every
    check, wash and sell route included). Size = a fixed share of current
    wallet equity, within max open positions and the daily loss cap.
  * Fills: a decision at time t fills at the first observation at or after
    t + delay, at that observation's price plus constant-product impact
    (``fills``). Never an earlier observation, so there is no look-ahead. No
    observation within the max wait = a gap, never a guessed price.
  * Exits: take-profit, stop-loss, time stop, liquidity collapse (the same
    ``exit_trigger`` the research replay uses); they fill after the delay too.
  * Honeypots: a deterministic share of entries can't be sold; the sale is a
    failed sell in the wallet (total loss, tip still paid).
  * Risk: the wallet's kill switch (drawdown from peak) or minimum equity cooks
    the run. Trading then stops until ``python reset.py``; the scanner keeps
    observing.

Paper money only: nothing here holds keys, signs or sends anything.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .db import event, now_iso
from .fills import ExitRule, Obs, cp_value_after_impact, exit_trigger, is_honeypot, model_from_config
from .wallet import InsufficientFunds, MockWallet, RunCooked, WalletConfig, q_down


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Executor:
    def __init__(self, conn, cfg: dict, wall=time.time, wallet: MockWallet | None = None):
        self.conn, self.cfg, self.wall = conn, cfg, wall
        self.tc = cfg["trading"]
        self.model = model_from_config(cfg)
        ex = self.tc["exit"]
        self.rule = ExitRule(ex["take_profit"], ex["stop_loss"], ex["time_stop_min"] * 60,
                             ex["liquidity_collapse"])
        self.wallet = wallet or MockWallet(cfg["db_path"], WalletConfig(**cfg["wallet"]))
        self.halted = False
        self._sizer_noted: set[tuple[str, str]] = set()
        self.wallet.unsellable |= {r[0] for r in conn.execute(
            "SELECT position_id FROM trades WHERE honeypot = 1 AND status IN ('open', 'pending_exit')")}

    # ---------------------------------------------------------------- data
    def on_chain(self, pair: str) -> bool:
        """Pairs watch.py reads on-chain every few seconds (and validated): one source per
        pair, so provider readings that lag up to 30 s can't trip an exit on stale prices."""
        r = self.conn.execute("SELECT 1 FROM pool_reads WHERE pair_address = ? AND supported = 1 "
                              "AND validated = 1", (pair,)).fetchone()
        return r is not None

    def observations(self, pair: str, after: float) -> list[Obs]:
        cut = datetime.fromtimestamp(after, timezone.utc).isoformat(timespec="milliseconds")
        table = "position_marks" if self.on_chain(pair) else "pairs_seen"
        return [Obs(_ts(r["observed_at"]), r["price_usd"], r["liquidity_usd"])
                for r in self.conn.execute(
                    f"SELECT observed_at, price_usd, liquidity_usd FROM {table} "
                    "WHERE pair_address = ? AND observed_at > ? ORDER BY id", (pair, cut))]

    def latest_obs(self, pair: str):
        return self.conn.execute(
            "SELECT observed_at, price_usd, liquidity_usd FROM pairs_seen WHERE pair_address = ? "
            "ORDER BY id DESC LIMIT 1", (pair,)).fetchone()

    def _fill_obs(self, pair: str, decided_at: float) -> tuple[str, Obs | None]:
        """('fill', obs) | ('wait', None) | ('gap', None) for a decision at decided_at."""
        due = decided_at + self.model.delay_s
        for o in self.observations(pair, due - 1e-3):
            if o.t >= due and o.price:
                return ("fill", o) if o.t - due <= self.model.max_fill_wait_s else ("gap", None)
        return ("gap", None) if self.wall() - due > self.model.max_fill_wait_s else ("wait", None)

    # ---------------------------------------------------------------- sizer + risk
    def size(self):
        w = self.wallet
        if w.is_cooked:
            return None
        c = w.config
        target = max(q_down(w.equity() * c.position_fraction), c.min_trade_size)
        s = min(target, w.max_affordable_notional())
        return s if s >= c.min_trade_size else None

    def realized_today(self) -> float:
        day = datetime.fromtimestamp(self.wall(), timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = self.conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status IN "
                              "('closed', 'failed_sell') AND exit_fill_t >= ?", (day,)).fetchone()
        return float(r[0])

    def _note_skip(self, pair: str, reason: str, detail: str):
        if (pair, reason) in self._sizer_noted:
            return
        self._sizer_noted.add((pair, reason))
        self.conn.execute("INSERT INTO skips (pair_address, ts, stage, reason, detail, metrics) "
                          "VALUES (?, ?, 'sizer', ?, ?, NULL)", (pair, now_iso(), reason, detail))

    # ---------------------------------------------------------------- loop
    def tick(self) -> dict:
        out = {"entries": 0, "fills": 0, "exits": 0, "halted": False}
        self._settle_closed()
        if self.wallet.is_cooked:
            self._halt(self.wallet.cooked_reason or "cooked")
            out["halted"] = True
            return out
        try:
            out["fills"] += self._fill_entries()
            self._monitor()
            out["exits"] += self._fill_exits()
            out["entries"] += self._new_entries()
        except RunCooked as e:
            self._halt(e.cause)
        if self.wallet.is_cooked:
            self._halt(self.wallet.cooked_reason or "cooked")
        out["halted"] = self.halted
        return out

    def _halt(self, cause: str):
        self._settle_closed()
        if not self.halted:
            self.halted = True
            event(self.conn, "trading_halted", f"wallet run {self.wallet.run_id} is cooked: "
                  f"{cause}. Trading stopped; run `python reset.py` to start a new run.")

    def _settle_closed(self):
        """Bring ``trades`` in line with the wallet, every tick. When a run cooks, the
        wallet liquidates its open positions at their last mark, and that run's pending
        entries will never fill. This must hold even if the run was cooked and reset
        before the executor saw it (e.g. Start new run pressed between ticks)."""
        for r in self.conn.execute("""
                SELECT t.id, p.status pstatus, p.closed_at, p.quote_exit, p.fill_exit, p.entry_fee,
                       p.exit_fee, p.entry_tip, p.exit_tip, p.proceeds, p.pnl, p.close_reason
                FROM trades t JOIN wallet_positions p USING (position_id)
                WHERE t.status IN ('open', 'pending_exit') AND p.status != 'open'""").fetchall():
            m = lambda v: (v or 0) / 1e6           # wallet money is integer micro-dollars
            self.conn.execute(
                "UPDATE trades SET status = ?, exit_reason = 'kill_switch_liquidation', "
                "exit_fill_t = ?, exit_mid = ?, exit_price = ?, fees = ?, tips = ?, proceeds = ?, "
                "pnl = ?, note = ? WHERE id = ?",
                ("failed_sell" if r["pstatus"] == "failed" else "closed", _ts(r["closed_at"]),
                 r["quote_exit"] and float(r["quote_exit"]), r["fill_exit"] and float(r["fill_exit"]),
                 m(r["entry_fee"]) + m(r["exit_fee"]), m(r["entry_tip"]) + m(r["exit_tip"]),
                 m(r["proceeds"]), m(r["pnl"]),
                 f"{r['close_reason']} at the last mark (no price impact)", r["id"]))
        self.conn.execute(
            "UPDATE trades SET status = 'entry_failed', note = 'run cooked before the fill' "
            "WHERE status = 'pending_entry' AND (wallet_run_id IS NOT ? OR ?)",
            (self.wallet.run_id, int(self.wallet.is_cooked)))

    def _new_entries(self) -> int:
        tc = self.tc
        block = "" if tc["one_trade_per_pair"] else \
            "AND t.status IN ('pending_entry', 'open', 'pending_exit')"
        cands = self.conn.execute(f"""
            SELECT f.pair_address, p.name FROM filter_state f JOIN pairs p USING (pair_address)
            WHERE f.outcome = 'pass' AND NOT EXISTS (
                SELECT 1 FROM trades t WHERE t.pair_address = f.pair_address {block})
            ORDER BY f.updated_at DESC""").fetchall()
        n = 0
        for c in cands:
            pa = c["pair_address"]
            open_n = self.conn.execute("SELECT COUNT(*) FROM trades WHERE status IN "
                                       "('pending_entry', 'open', 'pending_exit')").fetchone()[0]
            if open_n >= tc["max_open_positions"]:
                self._note_skip(pa, "max_open_positions",
                                f"{open_n} positions open (max {tc['max_open_positions']})")
                continue
            today = self.realized_today()
            if today <= -tc["daily_loss_cap_usd"]:
                self._note_skip(pa, "daily_loss_cap",
                                f"realized ${today:.2f} today (cap -${tc['daily_loss_cap_usd']})")
                continue
            size = self.size()
            if size is None:
                self._note_skip(pa, "no_viable_size", "wallet can't fund a minimum-size trade")
                continue
            o = self.latest_obs(pa)
            now = self.wall()
            tid = self.conn.execute(
                "INSERT INTO trades (pair_address, name, wallet_run_id, status, entry_reason, "
                "notional, entry_signal_t) VALUES (?, ?, ?, 'pending_entry', ?, ?, ?)",
                (pa, c["name"], self.wallet.run_id, "passed all filters", float(size), now)).lastrowid
            self.conn.execute("INSERT INTO signals (pair_address, t, kind, reason, price, liquidity, "
                              "trade_id) VALUES (?, ?, 'entry', ?, ?, ?, ?)",
                              (pa, now, "passed all filters", o and o["price_usd"],
                               o and o["liquidity_usd"], tid))
            n += 1
        return n

    def _fill_entries(self) -> int:
        n = 0
        for t in self.conn.execute("SELECT * FROM trades WHERE status = 'pending_entry'").fetchall():
            state, o = self._fill_obs(t["pair_address"], t["entry_signal_t"])
            if state == "wait":
                continue
            if state == "gap" or not o.liquidity:     # unknown, or an empty pool: no entry
                why = "no observation within the max fill wait" if state == "gap" \
                    else "no liquidity figure at fill time"
                self.conn.execute("UPDATE trades SET status = 'entry_failed', note = ? WHERE id = ?",
                                  (why, t["id"]))
                continue
            got = cp_value_after_impact(t["notional"], o.liquidity)
            price = o.price * t["notional"] / got
            try:
                pos = self.wallet.open_position(t["pair_address"], price, t["notional"])
            except InsufficientFunds as e:
                self.conn.execute("UPDATE trades SET status = 'entry_failed', note = ? WHERE id = ?",
                                  (f"wallet refused: {e}", t["id"]))
                continue
            hp = is_honeypot(self.model.honeypot_seed, t["pair_address"], o.t,
                             self.model.honeypot_rate)
            if hp:
                self.wallet.unsellable.add(pos["position_id"])
            self.conn.execute(
                "UPDATE trades SET status = 'open', position_id = ?, entry_fill_t = ?, "
                "entry_delay_s = ?, entry_mid = ?, entry_price = ?, entry_impact = ?, "
                "entry_liquidity = ?, honeypot = ?, last_obs_t = ? WHERE id = ?",
                (pos["position_id"], o.t, o.t - t["entry_signal_t"], o.price, price,
                 1 - got / t["notional"], o.liquidity, int(hp), o.t, t["id"]))
            n += 1
        return n

    def _monitor(self):
        for t in self.conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall():
            obs = self.observations(t["pair_address"], t["last_obs_t"])
            if not obs:
                continue
            last = obs[-1]
            if last.price:
                self.wallet.mark({t["position_id"]: last.price})
            for o in obs:
                reason = exit_trigger(self.rule, t["entry_mid"], t["entry_liquidity"],
                                      t["entry_fill_t"], o)
                if reason:
                    self.conn.execute(
                        "UPDATE trades SET status = 'pending_exit', exit_reason = ?, "
                        "exit_signal_t = ?, last_obs_t = ? WHERE id = ?",
                        (reason, o.t, o.t, t["id"]))
                    self.conn.execute(
                        "INSERT INTO signals (pair_address, t, kind, reason, price, liquidity, "
                        "trade_id) VALUES (?, ?, 'exit', ?, ?, ?, ?)",
                        (t["pair_address"], o.t, reason, o.price, o.liquidity, t["id"]))
                    break
            else:
                self.conn.execute("UPDATE trades SET last_obs_t = ? WHERE id = ?",
                                  (last.t, t["id"]))

    def _fill_exits(self) -> int:
        n = 0
        for t in self.conn.execute("SELECT * FROM trades WHERE status = 'pending_exit'").fetchall():
            state, o = self._fill_obs(t["pair_address"], t["exit_signal_t"])
            if state != "fill":
                if state == "gap":
                    # Can't fill honestly yet: keep the position, decide again from now.
                    self.conn.execute("UPDATE trades SET exit_signal_t = ?, note = ? WHERE id = ?",
                                      (self.wall(), "exit fill gap; retrying", t["id"]))
                continue
            pos = self.wallet.get_position(t["position_id"])
            if t["honeypot"]:
                closed = self.wallet.close_position(t["position_id"], failed=True,
                                                    reason=f"honeypot ({t['exit_reason']})")
                impact, eff = 1.0, 0.0
            else:
                if o.liquidity is None:
                    continue                      # unknown: wait for a reading (0 = empty pool, below)
                qty = float(pos["qty"])
                value = qty * o.price
                got = cp_value_after_impact(value, o.liquidity)
                eff = o.price * got / value if value else 0.0
                impact = 1 - got / value if value else 1.0
                closed = self.wallet.close_position(t["position_id"], eff, reason=t["exit_reason"])
            self.conn.execute(
                "UPDATE trades SET status = ?, exit_fill_t = ?, exit_delay_s = ?, exit_mid = ?, "
                "exit_price = ?, exit_impact = ?, fees = ?, tips = ?, proceeds = ?, pnl = ? "
                "WHERE id = ?",
                ("failed_sell" if t["honeypot"] else "closed", o.t, o.t - t["exit_signal_t"],
                 o.price, eff, impact, float(closed["entry_fee"] + (closed["exit_fee"] or 0)),
                 float(closed["entry_tip"] + (closed["exit_tip"] or 0)),
                 float(closed["proceeds"]), float(closed["pnl"]), t["id"]))
            n += 1
        return n

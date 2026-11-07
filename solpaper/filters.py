"""Phase 2 filters: decide whether each tracked pair may reach trading rules.

Cheapest first:
  tier1       stored observations only (no network): liquidity known and above
              the floor, quote token allowed, pair age in window, fresh data;
  token       Solana RPC mint read: mint authority, freeze authority,
              Token-2022 extensions that can block or tax a sale;
  sell_route  Jupiter quote: can our position size actually be sold, and at
              what price impact.

Within tier 1 the order is quote token, age, data freshness, then liquidity;
a pair is reported under the first check it fails.

A check that can't run (rate limit, outage) is a skip with reason
``check_unavailable:<check>``. A pair is never passed on missing data.
Decisions are logged when a pair's outcome or reason changes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import dex_label
from .db import event, now_iso
from .providers.jupiter import Jupiter
from .providers.solana_rpc import SolanaRpc, TokenCheck, risky_extensions


@dataclass
class Decision:
    pair_address: str
    outcome: str                  # pass | skip
    stage: str | None = None      # tier1 | token | sell_route (for skips)
    reason: str | None = None
    detail: str = ""
    metrics: dict = field(default_factory=dict)


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def tier1(row, cfg: dict, now: datetime) -> Decision | None:
    """Checks on stored data only. Returns a skip Decision, or None to continue."""
    fc = cfg["filters"]
    pa = row["pair_address"]
    dex = dex_label(cfg, row["dex"])
    m = {"dex": dex, "liquidity_usd": row["liquidity_usd"], "quote_mint": row["quote_mint"]}
    if row["quote_mint"] not in fc["allowed_quotes"]:
        return Decision(pa, "skip", "tier1", "quote_not_allowed",
                        f"priced in {row['name'] or row['quote_mint']}, not SOL/USDC", m)
    created = _ts(row["pair_created_at"])
    if created is None:
        return Decision(pa, "skip", "tier1", "age_unknown", "provider gave no creation time", m)
    age_min = (now - created).total_seconds() / 60
    m["age_min"] = round(age_min, 1)
    if age_min < fc["min_age_min"]:
        return Decision(pa, "skip", "tier1", "too_new",
                        f"{age_min:.1f} min old < {fc['min_age_min']} min", m)
    if age_min > fc["max_age_hours"] * 60:
        return Decision(pa, "skip", "tier1", "too_old",
                        f"{age_min / 60:.1f} h old > {fc['max_age_hours']} h", m)
    seen = _ts(row["observed_at"])
    read_age = (now - seen).total_seconds() if seen else None
    m["reading_age_s"] = None if read_age is None else round(read_age)
    if read_age is None or read_age > fc["max_reading_age_s"]:
        return Decision(pa, "skip", "tier1", "stale_data",
                        f"newest new reading {read_age:.0f}s old > {fc['max_reading_age_s']}s"
                        if read_age is not None else "no new reading", m)
    liq = row["liquidity_usd"]
    if liq is None:
        return Decision(pa, "skip", "tier1", "liquidity_unknown",
                        f"no liquidity figure (dex={dex})", m)
    if liq < fc["liquidity_floor_usd"]:
        return Decision(pa, "skip", "tier1", "liquidity_below_floor",
                        f"${liq:,.0f} < floor ${fc['liquidity_floor_usd']:,.0f}", m)
    return None


def token_decision(pa: str, tc: TokenCheck, fc: dict, m: dict) -> Decision | None:
    m = {**m, "mint_authority": tc.mint_authority, "freeze_authority": tc.freeze_authority,
         "program": tc.program}
    if not tc.readable:
        return Decision(pa, "skip", "token", "mint_unreadable",
                        "mint account missing or not a token mint", m)
    if tc.mint_authority:
        return Decision(pa, "skip", "token", "mint_authority_active",
                        f"creator can still mint ({tc.mint_authority})", m)
    if tc.freeze_authority:
        return Decision(pa, "skip", "token", "freeze_authority_active",
                        f"creator can freeze holders ({tc.freeze_authority})", m)
    bad = risky_extensions(tc, fc["risky_extensions"])
    if bad:
        return Decision(pa, "skip", "token", f"token_extension_risk:{bad[0]}",
                        f"Token-2022 extension(s) armed: {', '.join(bad)}", {**m, "risky": bad})
    return None


class Filters:
    def __init__(self, conn, http, cfg: dict, wall=time.time):
        self.conn, self.cfg, self.wall = conn, cfg, wall
        self.fc = cfg["filters"]
        self.rpc = SolanaRpc(http, cfg)
        self.jup = Jupiter(http, cfg)

    # ---------------------------------------------------------------- inputs
    def latest_rows(self, pairs: list[str]):
        """Each pair's newest reading that is genuinely new (unchanged = 0)."""
        if not pairs:
            return []
        q = ",".join("?" * len(pairs))
        return self.conn.execute(f"""
            SELECT p.pair_address, p.dex, p.name, p.base_mint, p.quote_mint, p.pair_created_at,
                   s.observed_at, s.liquidity_usd, s.price_usd, s.price_native
            FROM pairs p
            LEFT JOIN pairs_seen s ON s.id = (
                SELECT MAX(id) FROM pairs_seen x
                WHERE x.pair_address = p.pair_address AND x.unchanged = 0)
            WHERE p.pair_address IN ({q})""", pairs).fetchall()

    # ---------------------------------------------------------------- token cache
    def _cached_tokens(self, mints: list[str]) -> dict[str, TokenCheck]:
        out = {}
        ttl = self.fc["token_check_ttl_s"]
        for mint in mints:
            r = self.conn.execute("SELECT * FROM token_checks WHERE mint = ?", (mint,)).fetchone()
            if r is None:
                continue
            tc = TokenCheck(mint, bool(r["readable"]), r["program"], r["decimals"],
                            r["mint_authority"], r["freeze_authority"],
                            json.loads(r["extensions"] or "{}"))
            # Permanent only if nothing about the result can change: both
            # authorities revoked (they can't be re-enabled) and none of the
            # watched extensions present at all. Token-2022 extensions are fixed
            # when a mint is created, so a mint without them can't gain them;
            # one that has them (e.g. a zero transfer fee) can be changed by its
            # authority and is re-read after the TTL (spec FR-11).
            permanent = tc.readable and not tc.mint_authority and not tc.freeze_authority \
                and not any(n in tc.extensions for n in self.fc["risky_extensions"])
            if permanent or self.wall() - r["checked_at"] < ttl:
                out[mint] = tc
        return out

    def _store_tokens(self, checks: dict[str, TokenCheck]):
        for tc in checks.values():
            self.conn.execute(
                "INSERT OR REPLACE INTO token_checks (mint, checked_at, readable, program, "
                "decimals, mint_authority, freeze_authority, extensions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tc.mint, self.wall(), int(tc.readable), tc.program, tc.decimals,
                 tc.mint_authority, tc.freeze_authority, json.dumps(tc.extensions)))

    # ---------------------------------------------------------------- sell route
    def _sell_route(self, row, tc: TokenCheck, m: dict) -> Decision | None:
        pa, mint = row["pair_address"], row["base_mint"]
        r = self.conn.execute(
            "SELECT * FROM sell_quotes WHERE mint = ? AND checked_at > ? ORDER BY id DESC LIMIT 1",
            (mint, self.wall() - self.fc["sell_quote_ttl_s"])).fetchone()
        if r is None:
            if not row["price_usd"] or row["price_usd"] <= 0 or tc.decimals is None:
                return Decision(pa, "skip", "sell_route", "price_unknown",
                                "no price or token decimals to size the sell quote", m)
            q = self.jup.quote_sell(mint, tc.decimals, row["quote_mint"],
                                    self.fc["sell_check_usd"], row["price_usd"],
                                    row["price_native"], self.fc["sell_quote_slippage_bps"])
            if q is None:
                return Decision(pa, "skip", "sell_route", "check_unavailable:sell_route",
                                "Jupiter quote unavailable (rate limit, outage)", m)
            self.conn.execute(
                "INSERT INTO sell_quotes (pair_address, mint, ts, checked_at, usd_in, amount_in, "
                "out_mint, out_amount, price_impact, effective_vs_pool, routes, ok, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pa, mint, now_iso(), self.wall(), self.fc["sell_check_usd"], q.amount_in,
                 row["quote_mint"], q.out_amount, q.price_impact, q.effective_vs_pool,
                 q.routes, int(q.ok), q.reason))
            r = self.conn.execute("SELECT * FROM sell_quotes WHERE id = last_insert_rowid()"
                                  ).fetchone()
        m = {**m, "sell_impact": r["price_impact"], "effective_vs_pool": r["effective_vs_pool"],
             "routes": r["routes"]}
        if not r["ok"]:
            if (r["reason"] or "").startswith("no_route:"):
                return Decision(pa, "skip", "sell_route", "no_sell_route",
                                f"Jupiter: {r['reason'].split(':', 1)[1]}", m)
            return Decision(pa, "skip", "sell_route", "sell_quote_error",
                            f"Jupiter: {r['reason']}", m)
        if r["price_impact"] is not None and r["price_impact"] > self.fc["max_sell_impact"]:
            return Decision(pa, "skip", "sell_route", "sell_impact_too_high",
                            f"selling ${self.fc['sell_check_usd']} moves price "
                            f"{r['price_impact']:.1%} > {self.fc['max_sell_impact']:.0%}", m)
        return None

    # ---------------------------------------------------------------- main
    def evaluate(self, pairs: list[str], now: datetime | None = None) -> list[Decision]:
        now = now or datetime.now(timezone.utc)
        rows = self.latest_rows(pairs)
        decisions: dict[str, Decision] = {}
        survivors = []
        for row in rows:
            d = tier1(row, self.cfg, now)
            if d:
                decisions[row["pair_address"]] = d
            else:
                survivors.append(row)

        mints = list(dict.fromkeys(r["base_mint"] for r in survivors if r["base_mint"]))
        tokens = self._cached_tokens(mints)
        need = [m for m in mints if m not in tokens]
        if need:
            fetched = self.rpc.check(need)
            if fetched is not None:
                self._store_tokens(fetched)
                tokens.update(fetched)

        for row in survivors:
            pa = row["pair_address"]
            m = {"dex": dex_label(self.cfg, row["dex"]), "liquidity_usd": row["liquidity_usd"]}
            tc = tokens.get(row["base_mint"])
            if tc is None:
                decisions[pa] = Decision(pa, "skip", "token", "check_unavailable:token",
                                         "Solana RPC unavailable; not assumed safe", m)
                continue
            d = token_decision(pa, tc, self.fc, m) or self._sell_route(row, tc, m)
            decisions[pa] = d or Decision(pa, "pass", metrics={**m, "lp_lock": "not_checked"})
        for d in decisions.values():
            self._log(d)
        return list(decisions.values())

    def _log(self, d: Decision):
        prev = self.conn.execute("SELECT outcome, reason FROM filter_state WHERE pair_address = ?",
                                 (d.pair_address,)).fetchone()
        if prev is not None and (prev["outcome"], prev["reason"]) == (d.outcome, d.reason):
            return  # unchanged: already logged
        ts = now_iso()
        self.conn.execute(
            "INSERT OR REPLACE INTO filter_state (pair_address, outcome, reason, updated_at) "
            "VALUES (?, ?, ?, ?)", (d.pair_address, d.outcome, d.reason, ts))
        if d.outcome == "skip":
            self.conn.execute(
                "INSERT INTO skips (pair_address, ts, stage, reason, detail, metrics) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (d.pair_address, ts, d.stage, d.reason, d.detail, json.dumps(d.metrics)))
        else:
            self.conn.execute(
                "INSERT INTO filter_passes (pair_address, ts, metrics) VALUES (?, ?, ?)",
                (d.pair_address, ts, json.dumps(d.metrics)))
            event(self.conn, "filter_pass", f"{d.pair_address} passed all filters", d.metrics)


def evaluate_tracked(scanner, now: datetime | None = None) -> int:
    """Scanner job hook: evaluate every tracked pair. Returns decisions made."""
    f = getattr(scanner, "_filters", None)
    if f is None:
        f = scanner._filters = Filters(scanner.conn, scanner.http, scanner.cfg)
    return len(f.evaluate(scanner.tracked(), now))


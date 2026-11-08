"""Fast on-chain reads for the pairs the paper executor holds, then an executor tick.

Why: DexScreener's numbers refresh about every 30 s (its CDN caches responses),
so with provider data alone a stop-loss is seen up to 30 s late and an entry
fills up to a minute after the decision. Pumpswap pools are plain
constant-product pools, so their price is just the ratio of the pool's two
reserve accounts, which the public Solana RPC returns within about a second.

  * Read-only: ``getMultipleAccounts`` through the client's read-only RPC
    allow-list. Nothing here holds keys, signs or sends anything.
  * Pumpswap only (program ``pAMMBay6...``). Other pools (Raydium, Meteora,
    concentrated-liquidity and bonding-curve pools) aren't priced by their
    reserve ratio, so they keep the 30 s provider readings. Never guessed.
  * Layout verified 2026-11-08 against live pools: base/quote mints at bytes
    43/75, base/quote reserve accounts at 139/171; reserves matched
    DexScreener's price within 0.1-3% and its liquidity within 1-2%.
  * Each pool's first on-chain price must agree with the latest DexScreener
    price within ``max_disagreement`` before it's used. A moderate gap is
    usually DexScreener lagging a fast coin, so it's checked again on the next
    read; a 3x gap means a wrong decode, and the pair stays on provider readings.
  * Some pumpswap pools store the two tokens in the other order (SOL first);
    the reserve accounts are mapped to the pair's own base and quote.
  * USD: price in quote tokens x the quote token's USD price from the latest
    provider reading of the same pair (SOL moves little in 30 s; the coin is
    what moves). No recent quote price = no mark.
  * Readings go to ``position_marks``, never ``pairs_seen``, so filters,
    research and the study are unchanged.
"""

from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from datetime import datetime, timedelta, timezone

from .client import Http
from .config import dex_label
from .db import connect, event, now_iso

log = logging.getLogger("solpaper.watch")

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
POOL_BASE_MINT, POOL_QUOTE_MINT, POOL_BASE_VAULT, POOL_QUOTE_VAULT = 43, 75, 139, 171
POOL_MIN_LEN = POOL_QUOTE_VAULT + 32
TOKEN_ACCOUNT_MINT, TOKEN_ACCOUNT_AMOUNT = 0, 64        # SPL token account layout
MINT_DECIMALS = 44                                      # SPL mint layout
HELD = ("pending_entry", "open", "pending_exit")
MAX_ACCOUNTS = 100                                      # getMultipleAccounts limit
WRONG_READ = 3.0          # first on-chain price this many times off DexScreener's = wrong decode

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def address(raw: bytes) -> str:
    """Solana account address text for 32 raw bytes (the 58-letter alphabet).
    Used only to name accounts to read; there's no key material here."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _data(value: dict | None) -> bytes | None:
    if not value or not isinstance(value.get("data"), list):
        return None
    try:
        return base64.b64decode(value["data"][0])
    except (ValueError, TypeError):
        return None


def parse_pool(pool: dict | None, base_mint_acc: dict | None, quote_mint_acc: dict | None,
               base_mint: str, quote_mint: str) -> tuple[dict | None, str]:
    """(vault info, '') for a readable pumpswap pool, else (None, why not)."""
    d = _data(pool)
    if d is None:
        return None, "pool account not readable"
    if pool.get("owner") != PUMPSWAP_PROGRAM:
        return None, f"not a pumpswap pool (owner {pool.get('owner')})"
    if len(d) < POOL_MIN_LEN:
        return None, f"pool account too short ({len(d)} bytes)"
    mints = (address(d[POOL_BASE_MINT:POOL_BASE_MINT + 32]),
             address(d[POOL_QUOTE_MINT:POOL_QUOTE_MINT + 32]))
    vaults = (address(d[POOL_BASE_VAULT:POOL_BASE_VAULT + 32]),
              address(d[POOL_QUOTE_VAULT:POOL_QUOTE_VAULT + 32]))
    if mints == (quote_mint, base_mint):
        vaults = vaults[::-1]           # some pools store SOL first; keep the pair's orientation
    elif mints != (base_mint, quote_mint):
        return None, "pool mints don't match the pair"
    bm, qm = _data(base_mint_acc), _data(quote_mint_acc)
    if bm is None or qm is None or len(bm) <= MINT_DECIMALS or len(qm) <= MINT_DECIMALS:
        return None, "mint accounts not readable"
    return {"base_vault": vaults[0], "quote_vault": vaults[1],
            "base_decimals": bm[MINT_DECIMALS], "quote_decimals": qm[MINT_DECIMALS]}, ""


def reserves(info, base_acc: dict | None, quote_acc: dict | None) -> tuple[float, float] | None:
    """(base, quote) reserves in whole tokens, or None if either account is unreadable or
    isn't the pool's token account for the expected mint."""
    out = []
    for acc, mint, dec in ((base_acc, info["base_mint"], info["base_decimals"]),
                           (quote_acc, info["quote_mint"], info["quote_decimals"])):
        d = _data(acc)
        if d is None or len(d) < TOKEN_ACCOUNT_AMOUNT + 8 or \
                address(d[TOKEN_ACCOUNT_MINT:TOKEN_ACCOUNT_MINT + 32]) != mint:
            return None
        out.append(struct.unpack_from("<Q", d, TOKEN_ACCOUNT_AMOUNT)[0] / 10 ** dec)
    return out[0], out[1]


class PositionWatcher:
    def __init__(self, cfg: dict, conn=None, http: Http | None = None, executor=None):
        self.cfg, self.wc = cfg, cfg["watch"]
        self.conn = conn or connect(cfg["db_path"])
        self.http = http or Http(self.conn, cfg)
        self._executor = executor
        self._last_log = 0.0

    @property
    def executor(self):
        if self._executor is None:
            from .executor import Executor
            self._executor = Executor(self.conn, self.cfg)
        return self._executor

    def _rpc_accounts(self, addrs: list[str]) -> tuple[list, int | None] | None:
        r = self.http.rpc_read("solana_rpc", self.wc["bucket"], "getMultipleAccounts",
                               [addrs, {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(r, dict) or not isinstance(r.get("value"), list) or \
                len(r["value"]) != len(addrs):
            return None                     # a failure is already logged as a gap by the client
        return r["value"], (r.get("context") or {}).get("slot")

    # ---------------------------------------------------------------- pools
    def held_pairs(self) -> list:
        return self.conn.execute(f"""
            SELECT DISTINCT t.pair_address, p.dex, p.base_mint, p.quote_mint
            FROM trades t JOIN pairs p USING (pair_address)
            WHERE t.status IN {HELD}""").fetchall()

    def _lookup(self, pair) -> None:
        """Decide once per pair whether it can be read on-chain."""
        pa = pair["pair_address"]
        reason, info = "", None
        if dex_label(self.cfg, pair["dex"]) != "pumpswap":
            reason = f"{pair['dex']} pools aren't read on-chain (not a plain reserve-ratio pool)"
        elif not pair["base_mint"] or not pair["quote_mint"]:
            reason = "pair has no mint addresses"
        else:
            got = self._rpc_accounts([pa, pair["base_mint"], pair["quote_mint"]])
            if got is None:
                return                      # RPC gap: try again next tick
            info, reason = parse_pool(*got[0], pair["base_mint"], pair["quote_mint"])
        info = info or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO pool_reads (pair_address, supported, validated, reason, "
            "base_mint, quote_mint, base_vault, quote_vault, base_decimals, quote_decimals, "
            "checked_at) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pa, int(bool(info)), reason or None, pair["base_mint"], pair["quote_mint"],
             info.get("base_vault"), info.get("quote_vault"), info.get("base_decimals"),
             info.get("quote_decimals"), now_iso()))

    def _quote_usd(self, pa: str) -> tuple[float, float] | None:
        """(quote token USD price, provider price_native) from the latest recent provider reading."""
        cut = (datetime.now(timezone.utc) - timedelta(seconds=self.wc["quote_price_max_age_s"])
               ).isoformat(timespec="milliseconds")
        r = self.conn.execute(
            "SELECT price_usd, price_native FROM pairs_seen WHERE pair_address = ? AND "
            "observed_at >= ? AND price_usd > 0 AND price_native > 0 ORDER BY id DESC LIMIT 1",
            (pa, cut)).fetchone()
        return None if r is None else (r["price_usd"] / r["price_native"], r["price_native"])

    # ---------------------------------------------------------------- reads
    def read(self) -> int:
        held = self.held_pairs()
        known = {r["pair_address"]: r for r in self.conn.execute(
            "SELECT * FROM pool_reads WHERE pair_address IN (%s)" % ",".join("?" * len(held)),
            [h["pair_address"] for h in held])} if held else {}
        for h in held:
            if h["pair_address"] not in known:
                self._lookup(h)
        pools = [r for r in self.conn.execute(
            "SELECT * FROM pool_reads WHERE supported = 1 AND pair_address IN (%s)"
            % ",".join("?" * len(held)), [h["pair_address"] for h in held])] if held else []
        n = 0
        for k in range(0, len(pools), MAX_ACCOUNTS // 2):
            chunk = pools[k:k + MAX_ACCOUNTS // 2]
            got = self._rpc_accounts([a for p in chunk for a in (p["base_vault"], p["quote_vault"])])
            if got is None:
                continue
            accs, slot = got
            for i, p in enumerate(chunk):
                n += self._mark(p, reserves(p, accs[2 * i], accs[2 * i + 1]), slot)
        return n

    def _mark(self, pool, res, slot) -> int:
        pa = pool["pair_address"]
        if res is None or not res[0]:
            return 0                        # unreadable, or no base tokens left: no price
        base, quote = res
        native = quote / base
        q = self._quote_usd(pa)
        if q is None:
            return 0                        # no recent USD price for the quote token
        quote_usd, provider_native = q
        if not pool["validated"]:
            ratio = native / provider_native
            if not 1 / WRONG_READ < ratio < WRONG_READ:      # orders of magnitude: a wrong read
                self.conn.execute("UPDATE pool_reads SET supported = 0, reason = ? WHERE "
                                  "pair_address = ?", (f"on-chain price {ratio:.3g}x DexScreener's; "
                                                       "using provider readings", pa))
                event(self.conn, "watch_disagreement", f"{pa}: on-chain price {native:.4g} vs "
                      f"DexScreener {provider_native:.4g}; not using on-chain reads for it")
                return 0
            if abs(ratio - 1) > self.wc["max_disagreement"]:
                # Usually DexScreener lagging a fast coin: no mark yet, check again next read.
                self.conn.execute("UPDATE pool_reads SET reason = ? WHERE pair_address = ?",
                                  (f"waiting: on-chain {ratio - 1:+.0%} vs DexScreener", pa))
                return 0
            self.conn.execute("UPDATE pool_reads SET validated = 1, reason = NULL "
                              "WHERE pair_address = ?", (pa,))
        self.conn.execute(
            "INSERT INTO position_marks (pair_address, observed_at, slot, price_native, quote_usd, "
            "price_usd, liquidity_usd, base_reserve, quote_reserve) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pa, now_iso(), slot, native, quote_usd, native * quote_usd, 2 * quote * quote_usd,
             base, quote))
        return 1

    # ---------------------------------------------------------------- loop
    def tick(self) -> dict:
        marks = self.read()
        r = self.executor.tick()
        actions = r["entries"] + r["fills"] + r["exits"]
        if actions or time.monotonic() - self._last_log > 60:
            self._last_log = time.monotonic()
            log.info("positions        %d on-chain marks, %d trade actions", marks, actions)
        return {"marks": marks, **r}

    def run(self, should_stop) -> None:
        every = self.wc["interval_s"]
        while not should_stop():
            start = time.monotonic()
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep watching; one bad tick must not stop it
                log.exception("position watch failed")
                event(self.conn, "job_error", f"watch: {type(e).__name__}: {e}")
            while not should_stop() and time.monotonic() - start < every:
                time.sleep(0.2)


def start_thread(cfg: dict, should_stop) -> threading.Thread:
    """Run the watcher (with the paper executor) in its own thread, own DB connection
    and own request pacing, so slow scanner jobs can't delay a stop-loss check."""
    t = threading.Thread(target=lambda: PositionWatcher(cfg).run(should_stop),
                         name="position-watch", daemon=True)
    t.start()
    return t

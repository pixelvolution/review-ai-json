"""Fast on-chain reads of held pumpswap pools (watch.py) and how the executor uses them."""

import base64
import struct
from datetime import datetime, timezone

import pytest

from solpaper.config import load_config
from solpaper.db import connect, now_iso
from solpaper.executor import Executor
from solpaper.wallet import MockWallet, WalletConfig
from solpaper.watch import PUMPSWAP_PROGRAM, PositionWatcher, address

ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + ALPHA.index(ch)
    raw = n.to_bytes(32, "big")
    return raw


def key(i: int) -> tuple[bytes, str]:
    raw = bytes((i + k) % 256 for k in range(32))
    return raw, address(raw)


def acc(data: bytes, owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
    return {"owner": owner, "data": [base64.b64encode(bytes(data)).decode(), "base64"]}


class FakeRpc:
    def __init__(self):
        self.accounts, self.calls, self.down = {}, [], False

    def rpc_read(self, provider, bucket, method, params, fresh_s=0):
        assert method == "getMultipleAccounts" and bucket == "positions"
        self.calls.append(params[0])
        if self.down:
            return None
        return {"context": {"slot": 4242}, "value": [self.accounts.get(a) for a in params[0]]}


def pumpswap(rpc, pool_i=1, base_amount=1_000_000 * 10**6, quote_amount=10 * 10**9):
    (pool_raw, pool), (bm_raw, bm), (qm_raw, qm) = key(pool_i), key(pool_i + 40), key(pool_i + 80)
    (bv_raw, bv), (qv_raw, qv) = key(pool_i + 120), key(pool_i + 160)
    data = bytearray(301)
    data[43:75], data[75:107], data[139:171], data[171:203] = bm_raw, qm_raw, bv_raw, qv_raw
    rpc.accounts[pool] = acc(data, owner=PUMPSWAP_PROGRAM)
    for mint, dec in ((bm, 6), (qm, 9)):
        m = bytearray(82)
        m[44] = dec
        rpc.accounts[mint] = acc(m)
    for vault, mint_raw, amount in ((bv, bm_raw, base_amount), (qv, qm_raw, quote_amount)):
        t = bytearray(165)
        t[0:32] = mint_raw
        struct.pack_into("<Q", t, 64, amount)
        rpc.accounts[vault] = acc(t)
    return pool, bm, qm, bv, qv


@pytest.fixture
def w(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "w.db")
    conn = connect(cfg["db_path"])
    rpc = FakeRpc()

    class Idle:
        def tick(self):
            return {"entries": 0, "fills": 0, "exits": 0, "halted": False}
    return {"cfg": cfg, "conn": conn, "rpc": rpc,
            "watcher": PositionWatcher(cfg, conn=conn, http=rpc, executor=Idle())}


def hold(conn, pa, dex, bm, qm, price_native=1e-5, price_usd=1.5e-3, status="open"):
    conn.execute("INSERT INTO pairs (pair_address, dex, base_mint, quote_mint, name, first_seen_at, "
                 "first_source) VALUES (?, ?, ?, ?, 'X / SOL', ?, 'test')", (pa, dex, bm, qm, now_iso()))
    conn.execute("INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, price_native, "
                 "liquidity_usd) VALUES (?, ?, 'ds_refresh', ?, ?, 3000)",
                 (pa, now_iso(), price_usd, price_native))
    conn.execute("INSERT INTO trades (pair_address, status) VALUES (?, ?)", (pa, status))


# ------------------------------------------------------------------ decoding

def test_address_text_matches_solana_addresses():
    assert address(bytes(32)) == "1" * 32                       # the system program
    wsol = "So11111111111111111111111111111111111111112"
    assert address(decode(wsol)) == wsol


# ------------------------------------------------------------------ reads

def test_a_held_pumpswap_pool_is_priced_from_its_reserves(w):
    pool, bm, qm, bv, qv = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm)                    # DexScreener: 1e-5 SOL, SOL $150
    assert w["watcher"].read() == 1
    r = w["conn"].execute("SELECT * FROM pool_reads").fetchone()
    assert (r["supported"], r["validated"], r["base_vault"], r["quote_vault"]) == (1, 1, bv, qv)
    m = w["conn"].execute("SELECT * FROM position_marks").fetchone()
    assert m["price_native"] == pytest.approx(10 / 1_000_000)   # 10 SOL / 1M tokens
    assert m["price_usd"] == pytest.approx(1.5e-3)
    assert m["liquidity_usd"] == pytest.approx(2 * 10 * 150)    # both sides, like DexScreener
    assert m["slot"] == 4242
    w["watcher"].read()                                         # later reads: vaults only
    assert w["rpc"].calls[-1] == [bv, qv]


def test_other_pools_stay_on_provider_readings(w):
    _, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], "RAYPOOL", "raydium", bm, qm)
    assert w["watcher"].read() == 0
    r = w["conn"].execute("SELECT supported, reason FROM pool_reads").fetchone()
    assert r["supported"] == 0 and "raydium" in r["reason"]
    assert w["rpc"].calls == []                                 # nothing fetched for it


def test_a_pool_far_off_dexscreener_is_not_used(w):
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm, price_native=1e-3)   # chain says 1e-5: 100x off
    assert w["watcher"].read() == 0
    r = w["conn"].execute("SELECT supported, reason FROM pool_reads").fetchone()
    assert r["supported"] == 0 and "DexScreener" in r["reason"]
    assert w["conn"].execute("SELECT COUNT(*) FROM position_marks").fetchone()[0] == 0


def test_a_moderate_gap_waits_for_dexscreener_to_catch_up(w):
    # Job / SOL, 2026-11-08: DexScreener moved 21% in 22 s; the first check saw 29%.
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm, price_native=1.4e-5)  # chain 1e-5: -29%
    assert w["watcher"].read() == 0
    r = w["conn"].execute("SELECT supported, validated, reason FROM pool_reads").fetchone()
    assert (r["supported"], r["validated"]) == (1, 0) and "waiting" in r["reason"]
    w["conn"].execute("INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, "
                      "price_native) VALUES (?, ?, 'ds_refresh', 1.6e-3, 1.07e-5)", (pool, now_iso()))
    assert w["watcher"].read() == 1
    assert w["conn"].execute("SELECT validated FROM pool_reads").fetchone()[0] == 1


def test_a_pool_storing_sol_first_is_read_the_right_way_round(w):
    # TAOLF / SOL, 2026-11-08: the pool keeps SOL as its first token.
    pool, bm, qm, bv, qv = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", qm, bm, price_native=1e5)     # pair = SOL-side token / coin
    assert w["watcher"].read() == 1
    r = w["conn"].execute("SELECT base_vault, quote_vault FROM pool_reads").fetchone()
    assert (r["base_vault"], r["quote_vault"]) == (qv, bv)
    m = w["conn"].execute("SELECT price_native FROM position_marks").fetchone()
    assert m["price_native"] == pytest.approx(1_000_000 / 10)


def test_wrong_owner_or_mints_are_refused(w):
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    w["rpc"].accounts[pool]["owner"] = "SomeOtherProgram111111111111111111111111111"
    hold(w["conn"], pool, "pumpswap", bm, qm)
    w["watcher"].read()
    assert "not a pumpswap pool" in w["conn"].execute("SELECT reason FROM pool_reads").fetchone()[0]


def test_an_rpc_outage_writes_nothing_and_retries(w):
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm)
    w["rpc"].down = True
    assert w["watcher"].read() == 0
    assert w["conn"].execute("SELECT COUNT(*) FROM pool_reads").fetchone()[0] == 0
    w["rpc"].down = False
    assert w["watcher"].read() == 1


def test_no_recent_usd_price_for_the_quote_token_means_no_mark(w):
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm)
    w["conn"].execute("UPDATE pairs_seen SET observed_at = '2020-01-01T00:00:00.000+00:00'")
    assert w["watcher"].read() == 0


def test_closed_trades_are_not_watched(w):
    pool, bm, qm, _, _ = pumpswap(w["rpc"])
    hold(w["conn"], pool, "pumpswap", bm, qm, status="closed")
    assert w["watcher"].read() == 0 and w["rpc"].calls == []


# ------------------------------------------------------------------ executor on marks

T0 = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def test_executor_fills_and_stops_on_on_chain_marks_not_lagging_provider_data(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "x.db")
    cfg["fills"]["honeypot_rate"] = 0.0
    conn = connect(cfg["db_path"])
    clock = {"t": T0}
    ex = Executor(conn, cfg, wall=lambda: clock["t"],
                  wallet=MockWallet(cfg["db_path"], WalletConfig(**cfg["wallet"])))
    conn.execute("INSERT INTO pairs (pair_address, dex, name, first_seen_at, first_source) "
                 "VALUES ('A', 'pumpswap', 'A / SOL', ?, 'test')", (iso(T0),))
    conn.execute("INSERT INTO filter_state (pair_address, outcome, updated_at) VALUES ('A', 'pass', ?)",
                 (iso(T0),))
    ex.tick()                                              # decision at T0
    conn.execute("INSERT INTO pool_reads (pair_address, supported, validated, checked_at) "
                 "VALUES ('A', 1, 1, ?)", (iso(T0),))

    def mark(dt, price, liq=50_000.0):
        conn.execute("INSERT INTO position_marks (pair_address, observed_at, price_usd, liquidity_usd) "
                     "VALUES ('A', ?, ?, ?)", (iso(T0 + dt), price, liq))

    def provider(dt, price):
        conn.execute("INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, "
                     "liquidity_usd) VALUES ('A', ?, 'ds_refresh', ?, 50000)", (iso(T0 + dt), price))

    provider(6, 3.0)                     # a lagging provider print: must be ignored
    mark(6, 1.0)
    clock["t"] = T0 + 7
    ex.tick()
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["status"] == "open" and t["entry_mid"] == 1.0 and t["entry_delay_s"] == pytest.approx(6)
    provider(20, 3.0)                    # provider still says +200%
    mark(12, 0.7)                        # chain says -30%: stop-loss (25%)
    clock["t"] = T0 + 13
    ex.tick()
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["status"] == "pending_exit" and t["exit_reason"] == "stop_loss"
    mark(18, 0.68)
    clock["t"] = T0 + 19
    ex.tick()
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["status"] == "closed" and t["exit_mid"] == 0.68 and t["exit_delay_s"] == pytest.approx(6)


def test_if_the_watcher_dies_the_scanner_trades_again(tmp_path, monkeypatch):
    import solpaper.watch as watch
    from solpaper.scanner import Scanner

    class Broken:
        def __init__(self, cfg):
            raise RuntimeError("cannot start")
    monkeypatch.setattr(watch, "PositionWatcher", Broken)
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "s.db")
    sc = Scanner(cfg)
    sc._watch_thread = watch.start_thread(cfg, lambda: False)
    sc._watch_thread.join(5)
    assert sc.watching is False                       # trade job runs again
    assert sc.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'job_error' AND "
                           "message LIKE 'position watcher stopped%'").fetchone()[0] == 1

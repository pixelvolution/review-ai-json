"""Phase 1: request budget, backoff + gap logging, cache, providers, scanner."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from solpaper.budget import Backoff, Pacer
from solpaper.client import Http
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.providers.base import PairSnapshot
from solpaper.providers.dexscreener import parse_pair
from solpaper.providers.geckoterminal import parse_pool
from solpaper.scanner import Scanner, is_volume_spike

FIX = Path(__file__).parent / "fixtures"
PKG = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c["db_path"] = str(tmp_path / "t.db")
    return c


def make_http(cfg, handler, clock=None):
    clock = clock or FakeClock()
    conn = connect(cfg["db_path"])
    http = Http(conn, cfg, transport=httpx.MockTransport(handler), clock=clock,
                wall=lambda: clock.t, sleep=clock.sleep)
    return conn, http, clock


# ------------------------------------------------------------------ budget

def test_pacer_spaces_requests_under_the_cap():
    clk = FakeClock()
    p = Pacer(rpm=60, safety=0.5, clock=clk, sleep=clk.sleep)  # 30/min -> one per 2s
    for _ in range(31):
        p.wait()
    assert clk.t - 1000.0 == pytest.approx(60.0)  # 31 requests take 60s: never > 30/min
    assert all(s == pytest.approx(2.0) for s in clk.slept)


def test_backoff_is_exponential_capped_and_honours_retry_after():
    clk = FakeClock()
    b = Backoff(base_s=2, max_s=30, jitter=0, clock=clk)
    assert [b.failure() for _ in range(6)] == [2, 4, 8, 16, 30, 30]
    assert b.blocked
    assert b.failure(retry_after=120) == 120
    assert b.success() == 7 and not b.blocked


# ------------------------------------------------------------------ client

def test_429_backs_off_logs_a_gap_and_returns_nothing(cfg):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "10"})
        return httpx.Response(200, json={"data": []})

    conn, http, clk = make_http(cfg, handler)
    assert http.get_json("geckoterminal", "keyless", "/x") is None
    # While backing off, nothing is sent and nothing is invented.
    assert http.get_json("geckoterminal", "keyless", "/x") is None
    assert len(calls) == 1
    gap = conn.execute("SELECT * FROM data_gaps").fetchone()
    assert gap["provider"] == "geckoterminal" and gap["last_status"] == 429
    assert gap["ended_at"] is None

    clk.t += 11
    assert http.get_json("geckoterminal", "keyless", "/x") == {"data": []}
    gap = conn.execute("SELECT * FROM data_gaps").fetchone()
    assert gap["ended_at"] is not None
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["data_gap", "data_gap_end"]


def test_network_errors_and_outages_are_gaps_not_data(cfg):
    def handler(req):
        raise httpx.ConnectError("down")

    conn, http, clk = make_http(cfg, handler)
    assert http.get_json("dexscreener", "fast", "/x") is None
    row = conn.execute("SELECT status, error FROM request_log").fetchone()
    assert row["status"] is None and "ConnectError" in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


def test_cache_prevents_repeat_requests_within_freshness(cfg):
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json={"v": len(n)}, headers={"age": "7"})

    conn, http, clk = make_http(cfg, handler)
    assert http.get_json("dexscreener", "fast", "/p", fresh_s=30) == {"v": 1}
    assert http.last_age_s == 7  # CDN said 7s
    clk.t += 10
    assert http.get_json("dexscreener", "fast", "/p", fresh_s=30) == {"v": 1}
    assert len(n) == 1
    assert http.last_age_s == pytest.approx(17)  # CDN age + time in our cache
    clk.t += 25
    assert http.get_json("dexscreener", "fast", "/p", fresh_s=30) == {"v": 2}
    assert len(n) == 2
    assert conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] == 2


def test_missing_cdn_age_is_unknown_not_fresh(cfg):
    conn, http, clk = make_http(cfg, lambda r: httpx.Response(200, json={}))
    http.get_json("dexscreener", "fast", "/p", fresh_s=30)
    assert http.last_age_s is None


def test_repeated_identical_readings_are_flagged(cfg):
    from solpaper.providers.base import PairSnapshot as S
    sc = Scanner(cfg, conn=connect(cfg["db_path"]), http=None)
    snap = lambda p: S("P1", "ds_refresh", price_usd=p, vol={"m5": 1, "h1": 2},
                       buys={"h1": 3}, sells={"h1": 4})
    sc.record([snap(1.0)])
    sc.record([snap(1.0)])   # same numbers again
    sc.record([snap(1.1)])   # price moved
    flags = [r[0] for r in sc.conn.execute("SELECT unchanged FROM pairs_seen ORDER BY id")]
    assert flags == [0, 1, 0]


def test_old_database_gets_the_new_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE pairs_seen (id INTEGER PRIMARY KEY, pair_address TEXT, "
                "observed_at TEXT, source TEXT, volume_spike INTEGER NOT NULL DEFAULT 0)")
    old.commit()
    old.close()
    cols = {r["name"] for r in connect(str(path)).execute("PRAGMA table_info(pairs_seen)")}
    assert "unchanged" in cols


def test_non_retryable_errors_are_logged_without_backoff(cfg):
    conn, http, clk = make_http(cfg, lambda r: httpx.Response(404))
    assert http.get_json("dexscreener", "fast", "/missing") is None
    assert not http.backoffs["dexscreener"].blocked
    assert conn.execute("SELECT kind FROM events").fetchone()[0] == "http_error"


# ------------------------------------------------------------------ providers

def test_parse_geckoterminal_pool():
    d = json.loads((FIX / "gt_new_pools.json").read_text())
    s = parse_pool(d["data"][0], "gt_new_pools")
    a = d["data"][0]["attributes"]
    assert s.pair_address == a["address"]
    assert s.base_mint and not s.base_mint.startswith("solana_")
    assert s.pair_created_at == a["pool_created_at"]
    assert s.buys["h1"] == a["transactions"]["h1"]["buys"]
    # A missing number stays missing; it never becomes 0.
    assert (s.liquidity_usd is None) == (a["reserve_in_usd"] is None)


def test_parse_dexscreener_pair():
    d = json.loads((FIX / "ds_pairs.json").read_text())
    p = d["pairs"][0]
    s = parse_pair(p, "ds_refresh")
    assert s.pair_address == p["pairAddress"] and s.dex == p["dexId"]
    assert s.liquidity_usd == p["liquidity"]["usd"]
    assert s.vol["h24"] == p["volume"]["h24"]
    assert s.pair_created_at.startswith("20")


def test_volume_spike_needs_an_hour_of_history(cfg):
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    old = (now - timedelta(hours=3)).isoformat()
    young = (now - timedelta(minutes=10)).isoformat()
    s = PairSnapshot("P", "t", pair_created_at=old, vol={"m5": 5000, "h1": 6000})
    assert is_volume_spike(s, cfg, now)        # 5000 >= 3 * 500
    s.vol = {"m5": 1200, "h1": 6000}
    assert not is_volume_spike(s, cfg, now)    # 1200 < 1500
    s = PairSnapshot("P", "t", pair_created_at=young, vol={"m5": 5000, "h1": 5000})
    assert not is_volume_spike(s, cfg, now)    # too young to call it a spike


# ------------------------------------------------------------------ scanner

def test_scanner_logs_every_pair_and_writes_nothing_during_a_gap(cfg):
    gt = (FIX / "gt_new_pools.json").read_text()
    state = {"down": False}

    def handler(req):
        if state["down"]:
            return httpx.Response(503)
        if "new_pools" in req.url.path:
            return httpx.Response(200, text=gt)
        return httpx.Response(200, json={"pairs": []})

    conn, http, clk = make_http(cfg, handler)
    sc = Scanner(cfg, conn=conn, http=http)
    assert sc.run_job("gt_new_pools") == 2
    assert conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM pairs_seen").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='pair_new'").fetchone()[0] == 2

    # Served again from our cache inside the freshness window: not a new observation.
    assert sc.run_job("gt_new_pools") == 0
    assert conn.execute("SELECT COUNT(*) FROM pairs_seen").fetchone()[0] == 2

    state["down"] = True
    clk.t += 1000  # cache expired
    assert sc.run_job("gt_new_pools") is None
    assert conn.execute("SELECT COUNT(*) FROM pairs_seen").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM data_gaps").fetchone()[0] == 1


def test_each_job_is_timed_from_its_own_start(cfg):
    clk = FakeClock()
    sc = Scanner(cfg, conn=connect(cfg["db_path"]), http=None, clock=clk)
    ran = []

    def slow_job(job):  # every job takes 4 seconds
        ran.append((job, clk.t))
        clk.t += 4
        return 0

    sc.run_job = slow_job
    sc.tick()
    starts = dict(ran)
    for job, every in cfg["scanner"]["intervals_s"].items():
        assert sc.next_run[job] == starts[job] + every
    # The last job in the pass started 16s after the first; its next run is
    # a full interval after its own start, so the 25s quote cache has expired.
    last = list(cfg["scanner"]["intervals_s"])[-1]
    assert sc.next_run[last] - starts[last] > cfg["freshness_s"]["ds_pairs"]


# ------------------------------------------------------------------ safety

FORBIDDEN = [
    r"\bsolders\b", r"\bsolana\.(rpc|transaction|keypair)", r"\bnacl\b", r"\bbase58\b",
    r"Keypair", r"sendTransaction", r"sendRawTransaction", r"signTransaction",
    r"simulateTransaction", r"requestAirdrop", r"/swap\b(?!/v1/quote)", r"swap-instructions",
    r"private[_ ]?key", r"secret[_ ]?key", r"seed[_ ]?phrase", r"mnemonic",
    r"method=['\"]POST",
]


def test_no_code_path_can_sign_or_send_a_transaction():
    """No signing libraries, key handling, transaction or swap-building calls."""
    offenders = []
    posts = []
    for f in PKG.rglob("*.py"):
        if "tests" in f.parts:
            continue
        src = f.read_text()
        for pat in FORBIDDEN:
            if re.search(pat, src, re.IGNORECASE):
                offenders.append(f"{f.relative_to(PKG)}: {pat}")
        posts += [f"{f.relative_to(PKG)}" for _ in re.finditer(r"\.post\(", src)]
    assert offenders == []
    # Exactly one POST in the package: the allow-listed read-only RPC call.
    assert posts == ["client.py"]


def test_rpc_refuses_anything_but_read_only_methods(cfg):
    from solpaper.client import READ_ONLY_RPC_METHODS, ForbiddenRpcMethod
    sent = []
    conn, http, clk = make_http(cfg, lambda r: sent.append(r) or httpx.Response(200, json={}))
    for m in ("sendTransaction", "simulateTransaction", "requestAirdrop", "getBalance"):
        with pytest.raises(ForbiddenRpcMethod):
            http.rpc_read("solana_rpc", "rpc", m, [])
    assert sent == []
    assert READ_ONLY_RPC_METHODS == {"getAccountInfo", "getMultipleAccounts",
                                     "getTokenLargestAccounts", "getTokenSupply"}


def test_export_writes_emoji_names_as_utf8(cfg, tmp_path):
    from solpaper.export import export
    sc = Scanner(cfg, conn=connect(cfg["db_path"]), http=None)
    sc.record([PairSnapshot("P1", "ds_refresh", name="\U0001F319 BUCK / SOL", price_usd=1.0)])
    counts = export(cfg["db_path"], tmp_path, cfg, only="scanner")
    assert counts == {"readings.csv": 1, "pairs.csv": 1}
    raw = (tmp_path / "readings.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads it as UTF-8
    assert "\U0001F319 BUCK" in raw.decode("utf-8-sig")



def test_export_skips_tables_that_do_not_exist_yet(cfg, tmp_path):
    from solpaper.export import export
    connect(cfg["db_path"])                       # engine tables only; the study never ran
    counts = export(cfg["db_path"], tmp_path, cfg)
    assert counts["study_cohort.csv"] is None and counts["study_features.csv"] is None
    assert counts["filter_decisions.csv"] == 0     # table exists, no rows yet
    assert not (tmp_path / "study_cohort.csv").exists()
    readme = (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert "not a tradable edge" in readme and "Coverage is unknown" in readme

"""Phase 2: filters + skip-reason stats. IDs refer to specs/solpaper-phase2-filters/SPEC.md."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from solpaper.client import Http
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.filters import Filters
from solpaper.providers.solana_rpc import TokenCheck, parse_mint, risky_extensions

FIX = Path(__file__).parent / "fixtures"
PKG = Path(__file__).resolve().parents[1]
SOL = "So11111111111111111111111111111111111111112"
NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
CLEAN = {"data": {"program": "spl-token-2022", "parsed": {"type": "mint", "info": {
    "decimals": 6, "mintAuthority": None, "freezeAuthority": None, "supply": "1",
    "extensions": [{"extension": "metadataPointer", "state": {}}]}}}}


def mint_value(**info):
    base = json.loads(json.dumps(CLEAN))
    base["data"]["parsed"]["info"].update(info)
    return base


class Net:
    """Fake network: Solana RPC answers from `mints`, Jupiter from `quote`."""

    def __init__(self):
        self.mints: dict[str, dict | None] = {}
        self.quote = (200, json.loads((FIX / "jup_quote_ok.json").read_text()))
        self.rpc_status = 200
        self.calls = {"rpc": 0, "jup": 0}

    def __call__(self, req: httpx.Request):
        if req.url.host == "api.mainnet-beta.solana.com":
            self.calls["rpc"] += 1
            if self.rpc_status != 200:
                return httpx.Response(self.rpc_status)
            body = json.loads(req.content)
            assert body["method"] == "getMultipleAccounts"
            vals = [self.mints.get(m, CLEAN) for m in body["params"][0]]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"context": {"slot": 1}, "value": vals}})
        if req.url.host == "lite-api.jup.ag":
            self.calls["jup"] += 1
            assert req.url.path == "/swap/v1/quote"
            return httpx.Response(self.quote[0], json=self.quote[1])
        raise AssertionError(f"unexpected host {req.url.host}")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "t.db")
    conn = connect(cfg["db_path"])
    net, clk = Net(), Clock()
    http = Http(conn, cfg, transport=httpx.MockTransport(net), clock=clk,
                wall=lambda: clk.t, sleep=clk.sleep)
    f = Filters(conn, http, cfg, wall=lambda: clk.t)
    return {"cfg": cfg, "conn": conn, "net": net, "clk": clk, "f": f}


def add_pair(conn, pa, *, dex="pumpswap", liq=50_000.0, age_min=30, quote=SOL,
             reading_age_s=10, mint=None, unchanged=0, name="TEST / SOL", created=True):
    conn.execute(
        "INSERT OR IGNORE INTO pairs (pair_address, dex, base_mint, quote_mint, name, "
        "pair_created_at, first_seen_at, first_source) VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        (pa, dex, mint or f"MINT{pa}", quote, name,
         (NOW - timedelta(minutes=age_min)).isoformat() if created else None, NOW.isoformat()))
    conn.execute(
        "INSERT INTO pairs_seen (pair_address, observed_at, source, liquidity_usd, price_usd, "
        "price_native, unchanged) VALUES (?, ?, 'ds_refresh', ?, 0.001, 0.00001, ?)",
        (pa, (NOW - timedelta(seconds=reading_age_s)).isoformat(), liq, unchanged))


def decide(env, *pairs):
    return {d.pair_address: d for d in env["f"].evaluate(list(pairs), NOW)}


# ------------------------------------------------------------------ tier 1

def test_fr02_launchpad_without_liquidity_is_skipped(env):
    add_pair(env["conn"], "A", dex="pump-fun", liq=None)
    d = decide(env, "A")["A"]
    assert (d.outcome, d.reason) == ("skip", "liquidity_unknown")
    assert "dex=pump-fun" in d.detail


def test_fr03_liquidity_floor(env):
    add_pair(env["conn"], "A", liq=4_000)
    d = decide(env, "A")["A"]
    assert d.reason == "liquidity_below_floor" and "$4,000" in d.detail and "$10,000" in d.detail


def test_fr04_age_window(env):
    add_pair(env["conn"], "NEW", age_min=1)
    add_pair(env["conn"], "OLD", age_min=7 * 60)
    add_pair(env["conn"], "UNK", created=False)
    d = decide(env, "NEW", "OLD", "UNK")
    assert d["NEW"].reason == "too_new"
    assert d["OLD"].reason == "too_old"
    assert d["UNK"].reason == "age_unknown"


def test_fr05_stale_data_ignores_repeated_readings(env):
    add_pair(env["conn"], "A", reading_age_s=600)
    add_pair(env["conn"], "A", reading_age_s=5, unchanged=1)  # a repeat: not new data
    d = decide(env, "A")["A"]
    assert d.reason == "stale_data" and "600s" in d.detail


def test_ec06_quote_must_be_sol_or_usdc(env):
    add_pair(env["conn"], "A", quote="MEMEcoinQuoteMint111111111111111111111111111", name="X / SPCX")
    assert decide(env, "A")["A"].reason == "quote_not_allowed"


def test_ec05_dex_labels_are_normalized(env, tmp_path, monkeypatch, capsys):
    from solpaper import stats
    from solpaper.export import export
    add_pair(env["conn"], "A", dex="pumpfun", liq=None)
    add_pair(env["conn"], "B", dex="pump-fun", liq=None)
    assert decide(env, "A")["A"].metrics["dex"] == "pump-fun"
    monkeypatch.setenv("SOLPAPER_DB", env["cfg"]["db_path"])
    stats.main()
    out = capsys.readouterr().out
    assert "dexes: pump-fun 2" in out and "pumpfun" not in out
    export(env["cfg"]["db_path"], tmp_path, env["cfg"])
    assert "pumpfun," not in (tmp_path / "readings.csv").read_text(encoding="utf-8-sig")


def test_fr01_tier1_skips_make_no_network_requests(env):
    add_pair(env["conn"], "A", liq=100)
    decide(env, "A")
    assert env["net"].calls == {"rpc": 0, "jup": 0}


# ------------------------------------------------------------------ token checks

def test_fr06_fr07_authorities_from_real_rpc_data():
    r = json.loads((FIX / "rpc_get_multiple_accounts.json").read_text())["result"]["value"]
    meme, usdc = parse_mint("m", r[0]), parse_mint("u", r[2])
    assert meme.readable and not meme.mint_authority and not meme.freeze_authority
    assert usdc.mint_authority and usdc.freeze_authority  # USDC keeps both: would be skipped


def test_fr06_mint_authority_active(env):
    add_pair(env["conn"], "A", mint="M1")
    env["net"].mints["M1"] = mint_value(mintAuthority="Creator111")
    assert decide(env, "A")["A"].reason == "mint_authority_active"


def test_fr07_freeze_authority_active(env):
    add_pair(env["conn"], "A", mint="M1")
    env["net"].mints["M1"] = mint_value(freezeAuthority="Creator111")
    assert decide(env, "A")["A"].reason == "freeze_authority_active"


@pytest.mark.parametrize("ext,state,risky", [
    ("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 500}}, True),
    ("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 0},
                           "olderTransferFee": {"transferFeeBasisPoints": 0}}, False),
    ("transferHook", {"programId": "Hook1111"}, True),
    ("transferHook", {"programId": None, "authority": "Admin111"}, True),  # presence counts
    ("permanentDelegate", {"delegate": "Thief111"}, True),
    ("permanentDelegate", {"delegate": None}, True),                      # presence counts
    ("defaultAccountState", {"accountState": "frozen"}, True),
    ("defaultAccountState", {"accountState": "initialized"}, False),
    ("nonTransferable", {}, True),
    ("pausableConfig", {"authority": "Admin111", "paused": False}, True),
])
def test_fr08_token2022_extensions(ext, state, risky):
    cfg = load_config()
    tc = TokenCheck("m", True, "spl-token-2022", 6, None, None, {ext: state})
    assert (risky_extensions(tc, cfg["filters"]["risky_extensions"]) == [ext]) is risky


def test_fr08_risky_extension_skips_the_pair(env):
    add_pair(env["conn"], "A", mint="M1")
    env["net"].mints["M1"] = mint_value(extensions=[
        {"extension": "transferHook", "state": {"programId": "Hook1111"}}])
    assert decide(env, "A")["A"].reason == "token_extension_risk:transferHook"


def test_ec01_rpc_outage_is_never_a_pass(env):
    add_pair(env["conn"], "A")
    env["net"].rpc_status = 503
    d = decide(env, "A")["A"]
    assert (d.outcome, d.reason) == ("skip", "check_unavailable:token")


def test_ec01_jsonrpc_error_inside_http_200_is_a_gap(env):
    add_pair(env["conn"], "A")
    net = env["net"]
    real = net.__call__

    def rpc_error(req):
        if req.url.host == "api.mainnet-beta.solana.com":
            net.calls["rpc"] += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32005, "message": "busy"}})
        return real(req)

    env["f"].rpc.http.clients["solana_rpc"]._transport = httpx.MockTransport(rpc_error)
    d = decide(env, "A")["A"]
    assert d.reason == "check_unavailable:token"
    gap = env["conn"].execute("SELECT provider, ended_at FROM data_gaps").fetchone()
    assert gap["provider"] == "solana_rpc" and gap["ended_at"] is None
    assert env["f"].rpc.http.backoffs["solana_rpc"].blocked
    assert env["conn"].execute("SELECT COUNT(*) FROM token_checks").fetchone()[0] == 0


def test_ec02_unreadable_mint(env):
    add_pair(env["conn"], "A", mint="M1")
    env["net"].mints["M1"] = None
    assert decide(env, "A")["A"].reason == "mint_unreadable"


def test_fr11_changeable_extensions_are_rechecked_after_ttl(env):
    add_pair(env["conn"], "A", mint="M1")
    env["net"].mints["M1"] = mint_value(extensions=[
        {"extension": "transferFeeConfig",
         "state": {"newerTransferFee": {"transferFeeBasisPoints": 0}}}])
    assert decide(env, "A")["A"].outcome == "pass"   # zero fee: allowed today
    env["net"].mints["M1"] = mint_value(extensions=[
        {"extension": "transferFeeConfig",
         "state": {"newerTransferFee": {"transferFeeBasisPoints": 900}}}])
    env["clk"].t += env["cfg"]["filters"]["token_check_ttl_s"] + 1
    assert decide(env, "A")["A"].reason == "token_extension_risk:transferFeeConfig"
    assert env["net"].calls["rpc"] == 2


def test_fr11_token_checks_are_cached(env):
    add_pair(env["conn"], "A", mint="M1")
    add_pair(env["conn"], "B", mint="M2")
    env["net"].mints["M2"] = mint_value(freezeAuthority="X")
    decide(env, "A", "B")
    assert env["net"].calls["rpc"] == 1
    env["clk"].t += 60
    decide(env, "A", "B")
    assert env["net"].calls["rpc"] == 1          # both still cached
    env["clk"].t += env["cfg"]["filters"]["token_check_ttl_s"]
    decide(env, "A", "B")
    assert env["net"].calls["rpc"] == 2          # only the non-clean one is re-read
    rpc_reqs = env["conn"].execute(
        "SELECT url FROM request_log WHERE provider = 'solana_rpc'").fetchall()
    assert len(rpc_reqs) == 2


# ------------------------------------------------------------------ sell route

def test_fr14_sell_route_ok_passes_with_lp_lock_not_checked(env):
    add_pair(env["conn"], "A")
    d = decide(env, "A")["A"]
    assert d.outcome == "pass"
    assert d.metrics["lp_lock"] == "not_checked"           # FR-09
    q = env["conn"].execute("SELECT * FROM sell_quotes").fetchone()
    assert q["ok"] == 1 and q["routes"] and q["price_impact"] is not None


def test_fr14_ec07_no_route_400_is_an_answer_not_an_outage(env):
    add_pair(env["conn"], "A")
    env["net"].quote = (400, json.loads((FIX / "jup_quote_not_tradable.json").read_text()))
    d = decide(env, "A")["A"]
    assert d.reason == "no_sell_route" and "TOKEN_NOT_TRADABLE" in d.detail
    assert env["conn"].execute("SELECT COUNT(*) FROM data_gaps").fetchone()[0] == 0


def test_fr14_sell_impact_too_high(env):
    add_pair(env["conn"], "A")
    q = json.loads((FIX / "jup_quote_ok.json").read_text())
    q["priceImpactPct"] = "0.08"
    env["net"].quote = (200, q)
    d = decide(env, "A")["A"]
    assert d.reason == "sell_impact_too_high" and "8.0%" in d.detail


def test_fr14_missing_price_impact_is_not_zero(env):
    add_pair(env["conn"], "A")
    q = json.loads((FIX / "jup_quote_ok.json").read_text())
    del q["priceImpactPct"]
    env["net"].quote = (200, q)
    d = decide(env, "A")["A"]
    assert (d.outcome, d.reason) == ("skip", "sell_quote_error")
    assert env["conn"].execute("SELECT price_impact FROM sell_quotes").fetchone()[0] is None


def test_ec07_other_client_errors_are_quote_errors_not_outages(env):
    add_pair(env["conn"], "A")
    env["net"].quote = (404, {"error": "not found"})
    d = decide(env, "A")["A"]
    assert d.reason == "sell_quote_error" and "HTTP 404" in d.detail
    assert env["conn"].execute("SELECT COUNT(*) FROM data_gaps").fetchone()[0] == 0


def test_ec07_missing_price_is_price_unknown(env):
    add_pair(env["conn"], "A")
    env["conn"].execute("UPDATE pairs_seen SET price_usd = NULL")
    d = decide(env, "A")["A"]
    assert d.reason == "price_unknown"
    assert env["net"].calls["jup"] == 0


def test_fr14_quote_outage_is_check_unavailable(env):
    add_pair(env["conn"], "A")
    env["net"].quote = (503, {})
    assert decide(env, "A")["A"].reason == "check_unavailable:sell_route"


def test_fr14_quotes_are_cached_per_mint(env):
    add_pair(env["conn"], "A")
    decide(env, "A")
    env["clk"].t += 30
    decide(env, "A")
    assert env["net"].calls["jup"] == 1


# ------------------------------------------------------------------ logging

def test_fr10_skips_logged_only_on_change(env):
    add_pair(env["conn"], "A", liq=4_000)
    for _ in range(3):
        decide(env, "A")
    assert env["conn"].execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 1
    add_pair(env["conn"], "A", liq=None)          # new reading, new reason
    decide(env, "A")
    reasons = [r[0] for r in env["conn"].execute("SELECT reason FROM skips ORDER BY id")]
    assert reasons == ["liquidity_below_floor", "liquidity_unknown"]


def test_ec03_skip_then_pass_keeps_history(env):
    add_pair(env["conn"], "A", liq=4_000)
    decide(env, "A")
    add_pair(env["conn"], "A", liq=60_000)
    assert decide(env, "A")["A"].outcome == "pass"
    assert env["conn"].execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 1
    assert env["conn"].execute("SELECT COUNT(*) FROM filter_passes").fetchone()[0] == 1


def test_fr12_stats_show_skip_reasons(env, monkeypatch, capsys):
    from solpaper import stats
    add_pair(env["conn"], "A", liq=4_000)
    add_pair(env["conn"], "B", dex="pump-fun", liq=None)
    add_pair(env["conn"], "C")
    decide(env, "A", "B", "C")
    monkeypatch.setenv("SOLPAPER_DB", env["cfg"]["db_path"])
    stats.main()
    out = capsys.readouterr().out
    assert "pairs evaluated: 3   passed: 1" in out
    assert "liquidity_below_floor" in out and "liquidity_unknown" in out
    assert "sell-route quotes: 1" in out


def test_fr13_no_threshold_literals_in_filters():
    src = (PKG / "filters.py").read_text()
    code = re.sub(r'""".*?"""|#.*', "", src, flags=re.S)
    numbers = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", code))
    assert numbers <= {"0", "1", "60"}, numbers


def test_scanner_runs_the_filter_job(env):
    from solpaper.scanner import Scanner
    add_pair(env["conn"], "A", liq=4_000)
    sc = Scanner(env["cfg"], conn=env["conn"], http=env["f"].rpc.http)
    sc.tracked = lambda: ["A"]
    assert sc.run_job("filter") == 1

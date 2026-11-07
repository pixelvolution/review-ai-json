# FLAGGING REPORT
Feature: solpaper Phase 2: basic filters + skip-reason stats
Spec: specs/solpaper-phase2-filters/SPEC.md
Date: 2026-11-07
Referee: Flagging Agent

Build surveyed: commit `180502e` ("solpaper phase 2: basic filters, token checks, sell-route check").
The working tree is clean, so the build is already committed. It was surveyed with
`git show 180502e` / `git diff 0a6b993 180502e`, not as uncommitted changes.
Test run: `python -m pytest solpaper/tests -q`, **51 passed**.
Behaviours the tests don't cover were checked with a throwaway script (temporary DB,
mocked transport, no network, no repo writes). Its results are quoted as "Probe:".

---

## SCOREBOARD
| Result  | Count |
|---------|-------|
| PASS    | 15    |
| FLAG    | 6     |

FR/EC rulings: 21 (FR-01..FR-14, EC-01..EC-07).
Also: 4 interface flags (Data & Interfaces, outside the FR/EC count), 4 ghost items.

---

## FLAGS ON THE FIELD

### DRIFT — FR-08
**Spec says:**
> **Token-2022 risk extensions** (the "sell-safety where data allows" check we can do read-only): skip if the mint has any of `transferHook`, `permanentDelegate`, `nonTransferable`, `pausableConfig`, a `defaultAccountState` of frozen, or a `transferFeeConfig` with a non-zero fee (`token_extension_risk:<name>`).

**Acceptance criterion:**
> Tests per extension, using recorded mint data.

**What was found:**
solpaper/providers/solana_rpc.py:35-40
```python
        if name == "transferHook" and not st.get("programId"):
            continue
        if name == "permanentDelegate" and not st.get("delegate"):
            continue
        if name == "pausableConfig" and not (st.get("authority") or st.get("paused")):
            continue
```
solpaper/tests/test_phase2.py:172 `("transferHook", {"programId": None}, False),`
solpaper/tests/test_phase2.py:182 `tc = TokenCheck("m", True, "spl-token-2022", 6, None, None, {ext: state})`
Probe: `transferHook {'authority': 'Auth1', 'programId': None}` -> `risky? []`

**The foul:** The spec skips on the presence of `transferHook`, `permanentDelegate` or `pausableConfig`. The build only skips when they are "armed", so a mint with a hook authority but no hook program yet passes. The per-extension tests also use hand-written states, not recorded mint data: the only recorded fixture holds just `metadataPointer` and `tokenMetadata`.

---

### DRIFT — FR-11
**Spec says:**
> Token checks are cached in `token_checks` per mint. A revoked authority is cached for good (it can't be re-enabled). Any other result is re-checked after `token_check_ttl_s`.

**Acceptance criterion:**
> Test: second evaluation within TTL makes no RPC request.

**What was found:**
solpaper/filters.py:135-139
```python
            # Revoked authorities can't come back: a clean result never expires.
            clean = tc.readable and not tc.mint_authority and not tc.freeze_authority \
                and not risky_extensions(tc, self.fc["risky_extensions"])
            if clean or self.wall() - r["checked_at"] < ttl:
                out[mint] = tc
```
Probe: mint cached with an unarmed `transferHook`. After 10 x TTL the hook was armed on chain: `pass None rpc calls 1`.

**The foul:** The extension results, which are not revoked authorities, are cached for good with the rest of a "clean" result instead of being re-checked after `token_check_ttl_s`.

---

### DRIFT — FR-14
**Spec says:**
> Skip `sell_impact_too_high` when `priceImpactPct` exceeds `max_sell_impact`.

Technical Constraints: "Must follow existing patterns: `None` means unknown, never 0".

**Acceptance criterion:**
> Tests with recorded quote and no-route responses.

**What was found:**
solpaper/providers/jupiter.py:149
```python
            impact = float(data.get("priceImpactPct") or 0)
```
solpaper/filters.py:178
```python
        if r["price_impact"] is not None and r["price_impact"] > self.fc["max_sell_impact"]:
```
Probe: quote with `priceImpactPct` removed -> `pass`, stored `price_impact` = `0.0`.

**The foul:** A missing price impact is stored as 0 and the pair passes, which breaks the "None means unknown, never 0" constraint. The rest of FR-14 is met: GET `/swap/v1/quote`, `no_sell_route`, `sell_impact_too_high`, the `sell_quotes` row, the per-mint TTL cache, and the recorded fixtures.

---

### PARTIAL — EC-01
**Spec says:**
> RPC fails, rate-limits or times out for a tier-1 passer

**Acceptance criterion (Expected Behavior):**
> Pair is **not** passed. It is skipped with `check_unavailable:<check>` and retried next cycle. Never assumed safe. The gap is logged by the existing client.

**What was found:**
solpaper/client.py:166-177 (an HTTP 200 carrying a JSON-RPC error is treated as a success)
```python
        if status == 200:
            ...
                self._cache_put(key, provider, url, status, body, age)
                ...
                bo.success()
                self._gap_success(provider)
                return data
```
solpaper/client.py:129-132
```python
        if "error" in data:
            event(self.conn, "rpc_error", f"{provider} {method}: {data['error']}",
                  {"method": method})
            return None
```
Probe: 200 + `{"error": {"code": -32005, ...}}` -> `check_unavailable:token data_gaps 0 events ['rpc_error']`

**The foul:** The skip and the never-pass behaviour hold. But a JSON-RPC-level failure writes no gap: it closes any open gap, resets backoff, and is only logged as an `rpc_error` event.

---

### PARTIAL — EC-05
**Spec says:**
> Same coin appears under two DEX labels (`pump-fun` / `pumpfun`)

**Acceptance criterion (Expected Behavior):**
> DEX names are normalized to one label before any rule or stat uses them.

**What was found:**
solpaper/filters.py:50 `dex = fc["dex_aliases"].get(row["dex"], row["dex"])` (normalized inside filters only)
solpaper/scanner.py:72 `(s.pair_address, s.dex, s.base_mint, ...` (raw label stored)
solpaper/stats.py:32-33
```python
    print("dexes: " + ", ".join(f"{r[0]} {r[1]}" for r in q(
        "SELECT dex, COUNT(*) FROM pairs GROUP BY 1 ORDER BY 2 DESC LIMIT 8")))
```
Probe: `pairs.dex GROUP BY -> [('pump-fun', 1), ('pumpfun', 1)]`

**The foul:** Labels are normalized only inside filter decisions. The `stats` DEX breakdown, the stats sample table (stats.py:85) and the export (export.py:21) still use the raw labels.

---

### DRIFT — EC-07
**Spec says:**
> Jupiter returns HTTP 400 with an error code (e.g. `TOKEN_NOT_TRADABLE`)

**Acceptance criterion (Expected Behavior):**
> That is an answer, not an outage: skip `no_sell_route`. Only 429/5xx/network errors count as `check_unavailable:sell_route`.

**What was found:**
solpaper/client.py:196-199 (non-retryable, non-answer status)
```python
        else:
            event(self.conn, "http_error", f"{provider} {url} -> {status}",
                  {"url": url, "status": status})
        return None
```
solpaper/providers/jupiter.py:133-134
```python
        if not price_usd or price_usd <= 0 or decimals is None:
            return None
```
solpaper/filters.py:161-163
```python
            if q is None:
                return Decision(pa, "skip", "sell_route", "check_unavailable:sell_route",
                                "Jupiter quote unavailable (rate limit, outage or no price)", m)
```
Probe: HTTP 403 / 404 / 422 -> `check_unavailable:sell_route`; `price_usd` NULL -> `check_unavailable:sell_route` with 0 Jupiter calls.

**The foul:** HTTP 400 is handled correctly (`no_sell_route`, no gap). But non-400 4xx responses, a missing price or decimals, and a malformed 200 body (jupiter.py:147-151) also become `check_unavailable:sell_route`, which breaks "Only 429/5xx/network errors". The spec doesn't say where these cases should go instead.

---

## INTERFACE FLAGS (Data & Interfaces, outside the FR/EC count)

### DRIFT — D&I table columns
**Spec says:**
> `skips(id, pair_address, ts, stage, reason, detail, metrics_json)` ... `filter_passes(id, pair_address, ts, metrics_json)` ... `token_checks(mint, checked_at, mint_authority, freeze_authority, program, extensions_json, ok, reason)` ... `sell_quotes(id, pair_address, mint, ts, usd_in, amount_in, out_amount, out_mint, price_impact, effective_vs_pool, routes, ok, reason)`

**What was found:**
solpaper/db.py:91 `metrics      TEXT               -- JSON` (skips), db.py:99 `metrics      TEXT` (filter_passes)
solpaper/db.py:109-118 `token_checks`: `readable`, `decimals` and `extensions` present; `ok`, `reason` and `extensions_json` absent.
solpaper/db.py:125 `checked_at        REAL NOT NULL,` (sell_quotes, not in spec)

**The foul:** The table shapes differ from the spec: renamed columns, and `token_checks` has no `ok` or `reason`.

### DRIFT — D&I skip stage values
**Spec says:**
> Stage is `tier1` or `token`.

**What was found:**
solpaper/db.py:88 `stage        TEXT NOT NULL,     -- tier1 | token | sell_route`
solpaper/filters.py:162, 176, 179 `Decision(pa, "skip", "sell_route", ...`

**The foul:** A third stage value, `sell_route`, is written. The FR-14 amendment left the stage list unchanged.

### DRIFT — D&I config keys
**Spec says:**
> Config (`filters:`): `liquidity_floor_usd`, `min_age_min`, `max_age_hours`, `max_reading_age_s`, `allowed_quotes`, `risky_extensions`, `token_check_ttl_s`, `sell_check_usd`, `max_sell_impact`, `sell_quote_ttl_s`, `interval_s`.

**What was found:**
solpaper/config.yaml:67 `    filter: 30` (under `scanner.intervals_s`, not `filters.interval_s`)
solpaper/config.yaml:90-91 `  dex_aliases:` / `    pumpfun: pump-fun` (an unlisted key)

**The foul:** `filters.interval_s` is missing (the interval lives under `scanner.intervals_s.filter`), and an unlisted `dex_aliases` key was added.

### DRIFT — D&I provider interface
**Spec says:**
> New provider interface `TokenSafety.check(mints) -> dict[mint, TokenCheck] | None`, implemented by `providers/solana_rpc.py`.

**What was found:**
`grep -rn TokenSafety solpaper` returns nothing.
solpaper/providers/solana_rpc.py:68,74 `class SolanaRpc:` / `def check(self, mints: list[str]) -> dict[str, TokenCheck] | None:`

**The foul:** The signature matches, but no `TokenSafety` interface exists for `SolanaRpc` to implement.

---

## CLEAN PASSES

| ID    | Requirement                              | Verified by |
|-------|------------------------------------------|-------------|
| FR-01 | `filter` scanner job over tracked pairs, interval in config | scanner.py:137-139 `if job == "filter":`; config.yaml:67; filters.py:242-247; tests test_phase2.py:140-143 (tier-1 skip: `calls == {"rpc": 0, "jup": 0}`), 308-313 |
| FR-02 | No liquidity -> `liquidity_unknown`, DEX in detail | filters.py:73-76 `f"no liquidity figure (dex={dex})"`; test_phase2.py:100-104 |
| FR-03 | Liquidity floor                          | filters.py:77-79 `f"${liq:,.0f} < floor ${fc['liquidity_floor_usd']:,.0f}"`; test_phase2.py:107-110 |
| FR-04 | `too_new` / `too_old` / `age_unknown`    | filters.py:55-65; test_phase2.py:113-120 |
| FR-05 | Only `unchanged = 0`; `stale_data`       | filters.py:121 `x.unchanged = 0`, filters.py:69-72; test_phase2.py:123-127 |
| FR-06 | `getMultipleAccounts` jsonParsed, batch 100, `mint_authority_active` | solana_rpc.py:12, 77-80; filters.py:89-91; test_phase2.py:148-152 (recorded fixture), 155-158 |
| FR-07 | `freeze_authority_active`                | filters.py:92-94; test_phase2.py:148-152 (recorded fixture), 161-164 |
| FR-09 | `lp_lock = not_checked` on every pass     | filters.py:216 `metrics={**m, "lp_lock": "not_checked"}`; test_phase2.py:229 |
| FR-10 | Log on change; one pass row per streak    | filters.py:221-238; test_phase2.py:267-275 |
| FR-12 | stats "Filters" section, current reason, count and % | stats.py:57-67; test_phase2.py:287-298 |
| FR-13 | Thresholds in `config.yaml` `filters:`; no literals in filters.py | config.yaml:76-89; test_phase2.py:301-305 (only `0`, `1`, `60`) |
| EC-02 | Missing or non-mint account -> `mint_unreadable` | solana_rpc.py:52-60; filters.py:86-88; test_phase2.py:200-203 |
| EC-03 | Skip -> pass: new pass row, skip history kept | filters.py:224-238; test_phase2.py:278-284 |
| EC-04 | Pass -> fail: new skips row              | filters.py:224-234; no test; Probe: pass, then `liquidity_below_floor`, skips 1, filter_passes 1 |
| EC-06 | Non-SOL/USDC quote -> `quote_not_allowed` | filters.py:52-54; config.yaml:81-83; test_phase2.py:130-132 |

Technical constraints checked, no flag: reuses `client.Http` (client.py:104-133). The RPC allow-list is enforced by test (client.py:33, 120-121; test_phase1.py:285-292). The guard test still forbids write-style calls and allows exactly one `.post(` (test_phase1.py:258-281).

---

## GHOST CODE

| File                          | Lines   | No requirement found |
|-------------------------------|---------|----------------------|
| solpaper/stats.py             | 68-72   | Sell-route quote summary in stats (count, average impact, average effective price). FR-12 asks only for passed/skipped counts and reasons. |
| solpaper/filters.py           | 239     | `event(self.conn, "filter_pass", ...)`: an events row for each new pass streak. |
| solpaper/client.py            | 195, 197 | Phase 1 warning and `http_error` event text changed from `path` to the full `url`. The spec says "Must not modify Phase 1 scanner behavior except adding the `filter` job". |
| solpaper/providers/jupiter.py | 140     | Hard-coded `"slippageBps": 100` quote parameter, outside config. It doesn't affect any decision. |

---

## RECORD NOTES (no ruling)
- **Spec freeze.** Build commit `180502e` also edits SPEC.md. Besides the declared pre-build FR-14/EC-07 amendment, it adds the `filter_state` table, the provider-limit notes, and Open Question 3, which calls itself "Finding during the build". No FR or EC text changed after the build began.
- solpaper/stats.py:60 hard-codes `"the filter job runs every 30s"`. The real value is config.yaml:67.
- Tier-1 stops at the first failed check, in this order: quote, age, freshness, liquidity (filters.py:11-12). Launchpad pairs that fail an earlier check are counted under that reason, not `liquidity_unknown`. The spec sets no tier-1 order.

---

## VERDICT
FLAGS RAISED

Resolve all flags before this feature is considered complete.
Update SPEC.md if a flag represents a legitimate spec change — do not
silently fix code to pass a flag that reveals a wrong requirement.

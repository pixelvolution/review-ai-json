# FLAGGING REPORT (run 2)
Feature: solpaper Phase 2: basic filters + skip-reason stats
Spec: specs/solpaper-phase2-filters/SPEC.md (with amendments A1, A1b, A2, A3)
Date: 2026-11-07
Referee: Flagging Agent
Previous report: specs/solpaper-phase2-filters/FLAG-REPORT-2026-11-07.md (build `180502e`)

Build surveyed: HEAD `1c918e0`. The fix commits since the first report are `0fb0637`
("fix flagging-report findings; spec amendment A2") and `1c918e0` ("judge the sell
check on measured payout", A3). The working tree is clean.
Test run: `python -m pytest solpaper/tests -q`, **60 passed**.
Behaviours the tests don't cover were checked with throwaway scripts (temporary DB,
mocked transport, no network, no repo writes). Their results are quoted as "Probe:".

---

## SCOREBOARD
| Result  | Count |
|---------|-------|
| PASS    | 18    |
| FLAG    | 4     |

FR/EC rulings: 22 (FR-01..FR-14, FR-14a, EC-01..EC-07).
The 4 flagged IDs carry 6 flags: FR-08 (1), FR-12 (1), FR-14a (2), EC-07 (2).
Also: 1 interface flag (outside the FR/EC count) and 3 ghost items.

---

## FIRST-RUN FLAGS: STATUS

| First-run flag | Status | Evidence |
|---|---|---|
| DRIFT FR-08 (armed-only extensions; hand-written test states) | **PARTLY RESOLVED.** Presence now counts. The tests still don't use recorded mint data, so a flag remains (see PARTIAL FR-08). | solana_rpc.py:27-48; test_phase2.py:197-213 |
| DRIFT FR-11 (clean result cached for good) | **RESOLVED** | filters.py:143-145; test_phase2.py:257-268; Probe P9 |
| DRIFT FR-14 (missing impact stored as 0) | **RESOLVED.** An absent `priceImpactPct` is stored as NULL. Since A3 the decision uses the measured impact. | jupiter.py:111-114; test_phase2.py:339-343 |
| PARTIAL EC-01 (JSON-RPC error in HTTP 200 not a gap) | **RESOLVED** | client.py:127, 168-172, 189-197; test_phase2.py:230-248 |
| PARTIAL EC-05 (labels normalized in filters only) | **RESOLVED** | config.py:22-24; stats.py:33-38, 92; export.py:38-43; test_phase2.py:156-167 |
| DRIFT EC-07 (non-400 4xx, no price, malformed 200 -> outage) | **RESOLVED** as reported. The spec was corrected in A2 and the code matches the new cases. New EC-07 issues are flagged below. | jupiter.py:63-71; filters.py:166-168; test_phase2.py:346-367 |
| Interface: table columns | **RESOLVED** for skips, filter_passes and token_checks. A3 adds new sell_quotes columns, flagged below. | db.py:84-140 |
| Interface: stage values | **RESOLVED** (A2) | SPEC D&I; db.py:88 |
| Interface: config keys | **RESOLVED** (A2) | config.yaml:67, 76-92 |
| Interface: `TokenSafety` missing | **RESOLVED** | providers/base.py:54-67 |
| Ghost: stats quote summary | **RESOLVED.** Now backed by FR-12 (A2). | stats.py:74-79 |
| Ghost: `filter_pass` event | **RESOLVED.** Now backed by D&I (A2). | filters.py:261 |
| Ghost: client log text path -> url | **RESOLVED.** Now backed by the Technical Constraints note (A2). | client.py:197-199 |
| Ghost: hard-coded `slippageBps: 100` | **RESOLVED.** Now `sell_quote_slippage_bps` in config. | config.yaml:88; jupiter.py:59 |
| Record note: stats hard-codes "30s" | **RESOLVED** | stats.py:65-66 |

---

## FLAGS ON THE FIELD

### PARTIAL — FR-08
**Spec says:**
> **Token-2022 risk extensions** (the "sell-safety where data allows" check we can do read-only): skip if the mint has any of `transferHook`, `permanentDelegate`, `nonTransferable`, `pausableConfig`, a `defaultAccountState` of frozen, or a `transferFeeConfig` with a non-zero fee (`token_extension_risk:<name>`).

**Acceptance criterion:**
> Tests per extension, using recorded mint data.

**What was found:**
solpaper/tests/test_phase2.py:212
```python
    tc = TokenCheck("m", True, "spl-token-2022", 6, None, None, {ext: state})
```
solpaper/tests/test_phase2.py:21-23 (`CLEAN`, hand-written), 218-219 (`{"extension": "transferHook", "state": {"programId": "Hook1111"}}`, hand-written)
Fixture `rpc_get_multiple_accounts.json` extensions: `['metadataPointer', 'tokenMetadata']`, `['metadataPointer', 'tokenMetadata']`, `[]`.

**The foul:** The behaviour now matches the spec (solana_rpc.py:27-48). But every per-extension test still builds the state by hand. No recorded mint holds any of the six watched extensions.

---

### DRIFT — FR-12
**Spec says:**
> Skip-reason stats: `python -m solpaper.stats` adds a "Filters" section showing pairs passed and skipped, and each pair's **current** reason with count and %, plus a one-line summary of sell quotes (count, routes found, average impact, average sell price vs pool). *(A2: the quote summary was added.)*

**Acceptance criterion:**
> Test on a seeded DB.

**What was found:**
solpaper/stats.py:74-79
```python
        sq = q("SELECT COUNT(*) n, SUM(ok) ok, AVG(CASE WHEN ok THEN measured_impact END) imp, "
               "SUM(impact_disagrees) dis FROM sell_quotes")[0]
        if sq["n"]:
            imp = "n/a" if sq["imp"] is None else f"{sq['imp']:.2%}"
            print(f"sell-route checks: {sq['n']} ({sq['ok']} had a route)   avg measured impact "
                  f"{imp}   Jupiter's figure disagreed: {sq['dis'] or 0}")
```
Probe P11: `sell-route checks: 1 (1 had a route)   avg measured impact 0.00%   Jupiter's figure disagreed: 0`

**The foul:** Commit `1c918e0` removed "average sell price vs pool" from the summary line. A2 had added it to the spec, and A3 did not change FR-12. The summary also now has a disagreement count that FR-12 doesn't list.

---

### DRIFT — FR-14a
**Spec says:**
> A failed reference quote follows the same rules as the main quote.

**Acceptance criterion:**
> Tests: the reported-100%-but-payout-fine case passes and is flagged; the near-zero-payout case is skipped whatever Jupiter reports; a reference-quote outage gives `check_unavailable:sell_route`.

**What was found:**
solpaper/providers/jupiter.py:100-102
```python
        if ref_reason:
            return SellQuote(ok=False, reason=f"quote_error:reference {ref_reason}",
                             amount_in=amount)
```
solpaper/filters.py:192-196
```python
            if (r["reason"] or "").startswith("no_route:"):
                return Decision(pa, "skip", "sell_route", "no_sell_route", ...
            return Decision(pa, "skip", "sell_route", "sell_quote_error", ...
```
Probe P1: main quote HTTP 400 -> `no_sell_route`; reference quote HTTP 400 -> `sell_quote_error` (`Jupiter: quote_error:reference no_route:TOKEN_NOT_TRADABLE`).

**The foul:** A 400 on the reference quote is reported as `sell_quote_error`, but the same reply on the main quote is `no_sell_route`. That breaks "same rules". The other reference failures do match the main quote: 429/5xx -> `check_unavailable:sell_route` (P1c), other 4xx -> `sell_quote_error` (P1b). The three acceptance tests exist and pass (test_phase2.py:315-336).

---

### ASSUMPTION — FR-14a
**Spec says:**
> two quotes at the same moment, a `sell_reference_usd` sale and the `sell_check_usd` sale

**What was found:**
solpaper/config.yaml:42
```yaml
      quote: {rpm: 30, safety: 0.4}     # 12/min
```
solpaper/providers/jupiter.py:90, 96 (main quote, then reference quote, both through the paced bucket)
Probe P13 (pacer on the test clock): `t+0.0s amount=10000000000` / `t+5.0s amount=500000000` / `t+10.0s ...` / `t+15.0s ...`

**The foul:** Can't rule. The two quotes are sent one after the other, at least 5 s apart because of our own pacer. The spec doesn't say how far apart two quotes can be and still count as "the same moment".

---

### PARTIAL — EC-07
**Spec says:**
> Jupiter's reply to a sell quote is anything other than a complete quote

**Acceptance criterion (Expected Behavior):**
> Any other 4xx, or a 200 missing `outAmount`/`priceImpactPct`, is a skip with `sell_quote_error`; a missing value is never read as 0. [...] Only 429/5xx/network errors count as `check_unavailable:sell_route` and open a data gap.

**What was found:**
solpaper/providers/jupiter.py:63
```python
        status = data.get("_http_status")
```
solpaper/client.py:162-166, 189-196 (a 200 with an unparseable body goes down the outage path)
```python
        if status == 200:
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                err = f"bad JSON: {e}"
        ...
        if status in RETRYABLE or err:
            ...
            self._gap_failure(provider, status, err)
```
Probe P5: HTTP 200 with a JSON list body -> `AttributeError 'list' object has no attribute 'get'`. The exception leaves `evaluate()` before any decision is logged.
Probe P4: HTTP 200 with a non-JSON body -> `check_unavailable:sell_route`, `data_gaps` 1.

**The foul:** Not every incomplete 200 becomes `sell_quote_error`. A non-object JSON body crashes the whole filter cycle. A non-JSON body is treated as an outage and opens a gap, although it isn't a 429, a 5xx or a network error. The cases the spec names explicitly are met: 400, other 4xx, missing `outAmount`, `price_unknown` with no request (test_phase2.py:300-305, 346-367).

---

### ASSUMPTION — EC-07 / FR-14a
**Spec says (EC-07, A2):**
> a 200 missing `outAmount`/`priceImpactPct`, is a skip with `sell_quote_error`

**Spec says (FR-14a, A3):**
> Jupiter's `priceImpactPct` is stored as a cross-check only; it is NULL when absent

**What was found:**
solpaper/providers/jupiter.py:111-114
```python
        try:
            impact = float(data["priceImpactPct"])
        except (KeyError, TypeError, ValueError):
            impact = None  # cross-check only; unknown stays unknown, never 0
```
solpaper/tests/test_phase2.py:339-343 `assert decide(env, "A")["A"].outcome == "pass"   # decided on the measured payout`
Probe P3: missing `priceImpactPct` -> `pass`, stored `price_impact` NULL, `ok` 1.

**The foul:** Can't rule. EC-07 says a 200 without `priceImpactPct` is a `sell_quote_error` skip. FR-14a says the field is cross-check only and NULL when absent. A3 did not amend EC-07. The build follows FR-14a and passes the pair. Both readings are possible from the spec text, and neither FR-14a acceptance test settles it.

---

## INTERFACE FLAGS (Data & Interfaces, outside the FR/EC count)

### DRIFT — D&I `sell_quotes` columns
**Spec says:**
> `sell_quotes(id, pair_address, mint, ts, checked_at, usd_in, amount_in, out_mint, out_amount, price_impact, effective_vs_pool, routes, ok, reason)`.

**What was found:**
solpaper/db.py:131-135
```sql
    ref_usd_in        REAL,             -- tiny reference sale (amendment A3)
    ref_amount_in     INTEGER,
    ref_out_amount    INTEGER,
    measured_impact   REAL,             -- from payouts: what the decision uses
    impact_disagrees  INTEGER NOT NULL DEFAULT 0,
```
solpaper/db.py:172-177 (migration adds the same five columns)

**The foul:** The table has five columns the D&I shape doesn't list. FR-14a backs `impact_disagrees` and the measured impact. Nothing in the spec backs `ref_usd_in`, `ref_amount_in` or `ref_out_amount`, and A3 did not update D&I.

---

## CLEAN PASSES

| ID    | Requirement | Verified by |
|-------|-------------|-------------|
| FR-01 | `filter` scanner job; no network for tier-1 decisions | scanner.py:137-139; filters.py:264-269; config.yaml:67; test_phase2.py:170-173, 427-432 |
| FR-02 | No liquidity -> `liquidity_unknown`, `dex=` in detail | filters.py:75-78; test_phase2.py:121-125 |
| FR-03 | Liquidity floor, both numbers in detail | filters.py:79-81; test_phase2.py:128-131 |
| FR-04 | `too_new` / `too_old` / `age_unknown` | filters.py:57-67; test_phase2.py:134-141 |
| FR-05 | Only `unchanged = 0`; `stale_data` | filters.py:68-74, 123; test_phase2.py:144-148 |
| FR-06 | `getMultipleAccounts` jsonParsed, batches of 100; `mint_authority_active` | solana_rpc.py:12, 76-80; filters.py:91-93; test_phase2.py:178-188 (recorded fixture) |
| FR-07 | `freeze_authority_active` | filters.py:94-96; test_phase2.py:178-182 (recorded fixture), 191-194 |
| FR-09 | `lp_lock = not_checked` on every pass | filters.py:238; test_phase2.py:294 |
| FR-10 | Logged on change; one pass row per streak | filters.py:243-261; test_phase2.py:386-394 |
| FR-11 | Per-mint cache; revoked results permanent; others re-read after TTL | filters.py:127-147; test_phase2.py:257-285; Probe P9 (clean mint not re-read after 10x TTL, unreadable mint re-read) |
| FR-13 | Thresholds in `config.yaml` `filters:`; no literals in filters.py | config.yaml:76-92; test_phase2.py:420-424 |
| FR-14 | GET `/swap/v1/quote`, `sell_check_usd` into the pair's quote token, `no_sell_route`, `sell_quotes` row, per-mint TTL cache. The `priceImpactPct` decision sentence is read with FR-14a, whose first acceptance test (reported 100%, payout fine -> pass) can't hold under FR-14's wording. | jupiter.py:54-72, 86-128; filters.py:159-203, 235-237; test_phase2.py:290-305, 370-381 |
| EC-01 | RPC failure, timeout, or JSON-RPC error in 200 -> `check_unavailable:token`, gap, never a pass | client.py:127, 168-172, 189-197; test_phase2.py:223-248; Probe P10 (timeout -> gap 1) |
| EC-02 | Missing or non-mint account -> `mint_unreadable` | solana_rpc.py:51-59; filters.py:88-90; test_phase2.py:251-254 |
| EC-03 | Skip -> pass: new pass row, skip history kept | filters.py:243-261; test_phase2.py:397-403 |
| EC-04 | Pass -> fail: new skips row | filters.py:243-256; no test; Probe P8: `pass -> liquidity_below_floor skips 1 passes 1` |
| EC-05 | One DEX label in rules, stats and export | config.py:22-24; filters.py:52, 229; stats.py:33-38, 92; export.py:38-43; test_phase2.py:156-167 |
| EC-06 | Non-SOL/USDC quote -> `quote_not_allowed` | filters.py:54-56; config.yaml:80-82; test_phase2.py:151-153 |

FR-14a parts that pass: the formula (jupiter.py:105; Probe P7: stored 0.02, recalculated 0.02), the skip on `measured_impact > max_sell_impact` (filters.py:197), `impact_disagrees` above the tolerance (jupiter.py:126-127; Probe P7b/P7c: 4% -> 0, 6% -> 1), NULL when absent, and all three acceptance tests (test_phase2.py:315-336). The two flags above stop FR-14a from passing.

Technical constraints checked, no flag: reuses `client.Http`; RPC allow-list unchanged (client.py:33, 120-121); test_phase1.py unchanged since `180502e`; `TokenSafety` and `SellRoute` interfaces in providers/base.py:54-67.

---

## GHOST CODE

| File | Lines | No requirement found |
|------|-------|----------------------|
| solpaper/config.yaml | 69-70 | `track_hours` 6 -> 12 and `max_tracked` 300 -> 1000 (commit `1c918e0`). This is a Phase 1 scanner change. Technical Constraints: "Must not modify Phase 1 scanner behavior except adding the `filter` job." The Bible records user approval (Environment Notes), but no spec amendment does. |
| solpaper/providers/jupiter.py | 104-110 | Zero-payout rules: both sales pay 0 -> `measured = 1.0` -> `sell_impact_too_high` (Probe P6); the reference pays 0 but the main sale doesn't -> `quote_error:reference sale paid 0` (Probe P6b). FR-14a gives no rule for either. |
| solpaper/providers/jupiter.py | 88-89 | `quote_error:amount rounds to zero` when either sale size rounds to 0 base units. EC-07 doesn't list this case. |

---

## RECORD NOTES (no ruling)
- **Crash on legacy quote rows.** The migration (db.py:172-177) adds `measured_impact` as NULL to rows written before A3. If a pre-A3 row with `ok = 1` is still inside `sell_quote_ttl_s` (180 s) at upgrade, filters.py:197 `if r["measured_impact"] > self.fc["max_sell_impact"]:` raises. Probe P2: `TypeError '>' not supported between instances of 'NoneType' and 'float'`, `filter_state` rows 0. The whole cycle is lost, not just that pair. scanner.py:152 catches the exception, so this repeats each cycle until the row ages out. No spec ID covers migrated rows.
- **Spec housekeeping after A3.** The FR-14 sentence "Skip `sell_impact_too_high` when `priceImpactPct` exceeds `max_sell_impact`" still stands next to FR-14a. The D&I config list lacks `sell_reference_usd` and `impact_disagreement_tol`. FR-14a and the A3 log name both keys, and both are in config.yaml:90-91.
- **FR-11 inference.** A result is cached for good only if both authorities are revoked and none of the watched extensions is present (filters.py:137-145). This relies on Token-2022 extensions being fixed when a mint is created. The spec doesn't say so.
- **Spec freeze.** The A2 and A3 edits follow the record rule: A2 is logged as spec errors after the first report, and A3 as user-approved. Commit `1c918e0` changes SPEC.md by two lines only, the FR-14a row and the A3 log row.

---

## VERDICT
FLAGS RAISED

Resolve all flags before this feature is considered complete.
Update SPEC.md if a flag represents a legitimate spec change — do not
silently fix code to pass a flag that reveals a wrong requirement.

---

Notify Project Bible:
```
Flagging report for solpaper-phase2-filters (run 2): 6 flags raised on 4 IDs (FR-08, FR-12, FR-14a, EC-07), 1 interface flag, 3 ghost items.
Spec: specs/solpaper-phase2-filters/SPEC.md
Report: specs/solpaper-phase2-filters/FLAG-REPORT-2026-11-07-run2.md
```

Notify Anti-Vibe:
```
Ghost code detected in solpaper-phase2-filters — see FLAG-REPORT-2026-11-07-run2.md GHOST section.
```

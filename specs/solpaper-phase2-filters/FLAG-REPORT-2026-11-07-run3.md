# FLAGGING REPORT (run 3)
Feature: solpaper Phase 2: basic filters + skip-reason stats
Spec: specs/solpaper-phase2-filters/SPEC.md (with amendments A1, A1b, A2, A3, A4)
Date: 2026-11-07
Referee: Flagging Agent
Previous report: specs/solpaper-phase2-filters/FLAG-REPORT-2026-11-07-run2.md (build `1c918e0`)

Build surveyed: committed HEAD `5c3a3dd` ("fix flag-run-2 findings; spec amendment A4").
The working tree holds uncommitted work for another feature (solpaper/study/, a `study:`
config section, a `study` RPC bucket, specs/launch-outcome-study/,
fixtures/rpc_pump_curves.json). It was not judged. Tests and probes ran on a clean
`git archive HEAD` export, so the uncommitted config.yaml was not loaded.
Test run (HEAD export): `python -m pytest solpaper/tests/test_phase1.py solpaper/tests/test_phase2.py -q`, **65 passed**. The same command on the working tree also gives 65 passed.
Behaviours the tests don't cover were checked with a throwaway script (temporary DB,
mocked transport, test clock, no network, no repo writes). Its results are quoted as "Probe:".

---

## SCOREBOARD
| Result  | Count |
|---------|-------|
| PASS    | 20    |
| FLAG    | 2     |

FR/EC rulings: 22 (FR-01..FR-14, FR-14a, EC-01..EC-07).
Flagged IDs: FR-08 (PARTIAL), EC-07 (PARTIAL).
Also: 1 interface flag (outside the FR/EC count) and 1 ghost item.

---

## RUN-2 FLAGS: STATUS

| Run-2 flag | Status | Evidence |
|---|---|---|
| PARTIAL FR-08 (no recorded mint data for watched extensions) | **PARTLY RESOLVED.** A recorded PYUSD mint now covers `transferHook` (presence, null program), `permanentDelegate` and a 0% `transferFeeConfig`. `nonTransferable`, `pausableConfig`, `defaultAccountState` frozen and a non-zero `transferFeeConfig` are still tested only on hand-built states. The flag stays, narrowed. | test_phase2.py:437-444; fixtures/rpc_pyusd_extensions.json |
| DRIFT FR-12 ("avg sell price vs pool" dropped) | **RESOLVED.** The field is back on the summary line, and a test checks it. The extra disagreement count is still there; it moves to GHOST. | stats.py:74-82; test_phase2.py:469-475; Probe Q7 |
| DRIFT FR-14a (reference 400 -> `sell_quote_error`) | **RESOLVED.** A reference 400 is now `no_sell_route`. Every reference failure matches the main quote: 400, 404, 429, non-object, missing `outAmount`. | jupiter.py:102-107; test_phase2.py:456-460; Probe Q3 |
| ASSUMPTION FR-14a ("the same moment") | **RESOLVED** (A4 spec error). The spec now defines it as back to back, about one pacer interval or ~5 s. The build matches. | SPEC FR-14a; config.yaml:42; Probe Q5 |
| PARTIAL EC-07 (non-object JSON crash; non-JSON 200 as outage) | **RESOLVED** as reported. A non-object reply is `sell_quote_error` (test added). A4 makes a non-JSON 200 an outage, and the build does that. A new crash path under EC-07 is flagged below. | jupiter.py:63-64; test_phase2.py:463-466; Probes Q3, Q4 |
| ASSUMPTION EC-07 / FR-14a (missing `priceImpactPct`) | **RESOLVED** (A4 spec error). EC-07 now defers to FR-14a: stored NULL, decision on measured impact. The build matches. | SPEC EC-07; jupiter.py:116-119; test_phase2.py:339-343 |
| Interface DRIFT: `sell_quotes` columns | **RESOLVED** (A4). The D&I column list now matches db.py:120-140 column for column. | SPEC D&I; db.py:120-140, 172-177 |
| Ghost: tracking limits 300 -> 1000, 6 h -> 12 h | **RESOLVED.** Now backed by the A4 exception in Technical Constraints. The arithmetic checks out: 1000 / 30 per call = 34 calls per 30 s refresh = 68/min, against the `fast` bucket's 300 x 0.5 = 150/min. | SPEC Technical Constraints; config.yaml:24, 66, 69-70 |
| Ghost: zero-payout rules | **RESOLVED.** Now backed by FR-14a (A4). | jupiter.py:108-115; Probes Q6a, Q6b |
| Ghost: `amount rounds to zero` | **RESOLVED.** Now backed by FR-14a (A4). | jupiter.py:88-91; Probe Q6c |
| Record note: crash on pre-A3 quote rows | **RESOLVED for the crash.** An `ok = 1` row with NULL `measured_impact` is skipped by the cache query and re-quoted. The fix covers only `ok = 1` rows, though. Pre-A3 `ok = 0` rows are still decided on, which the A4 D&I text forbids. See the interface flag below. | filters.py:162-167; test_phase2.py:447-453; Probes Q1, Q1b |
| Record note: spec housekeeping after A3 | **RESOLVED** (A4). FR-14's threshold sentence is marked as superseded, and the D&I config list names `sell_reference_usd` and `impact_disagreement_tol`. | SPEC FR-14, D&I |

---

## FLAGS ON THE FIELD

### PARTIAL — FR-08
**Spec says:**
> **Token-2022 risk extensions** (the "sell-safety where data allows" check we can do read-only): skip if the mint has any of `transferHook`, `permanentDelegate`, `nonTransferable`, `pausableConfig`, a `defaultAccountState` of frozen, or a `transferFeeConfig` with a non-zero fee (`token_extension_risk:<name>`).

**Acceptance criterion:**
> Tests per extension, using recorded mint data.

**What was found:**
solpaper/tests/test_phase2.py:443-444 (recorded PYUSD mint)
```python
    assert risky_extensions(tc, cfg["filters"]["risky_extensions"]) == [
        "transferHook", "permanentDelegate"]           # 0% transfer fee is not counted
```
solpaper/tests/test_phase2.py:198, 205, 207-208 (hand-built states, still the only coverage for these cases)
```python
    ("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 500}}, True),
    ("defaultAccountState", {"accountState": "frozen"}, True),
    ("nonTransferable", {}, True),
    ("pausableConfig", {"authority": "Admin111", "paused": False}, True),
```

**The foul:** Recorded mint data now backs 2 of the 6 watched extensions plus the zero-fee case. `nonTransferable`, `pausableConfig`, `defaultAccountState` frozen and a non-zero `transferFeeConfig` still have no test on recorded mint data. The behaviour itself matches the spec (solana_rpc.py:27-48).

---

### PARTIAL — EC-07
**Spec says:**
> Jupiter's reply to a sell quote is anything other than a complete quote

**Acceptance criterion (Expected Behavior):**
> Any other 4xx, a 200 missing `outAmount`, or a 200 whose JSON isn't an object is `sell_quote_error`; a missing value is never read as 0.

**What was found:**
solpaper/providers/jupiter.py:126-127
```python
        routes = ",".join(dict.fromkeys(
            r.get("swapInfo", {}).get("label", "?") for r in data.get("routePlan") or []))
```
Probe Q2: a 200 JSON object with a valid `outAmount` but a malformed `routePlan` raises out of `evaluate()` before any decision is logged (`filter_state` rows 0):
- `routePlan: [{"swapInfo": null}]` -> `AttributeError 'NoneType' object has no attribute 'get'`
- `routePlan: [1]` -> `AttributeError 'int' object has no attribute 'get'`
- `routePlan: "x"` or `{"a": 1}` -> `AttributeError 'str' object has no attribute 'get'`

scanner.py:152-155 catches the exception, so the whole filter job is lost, not just that pair. No `sell_quotes` row is written, so the next cycle sends the quote again and fails the same way.

**The foul:** An incomplete 200 object still crashes the filter cycle instead of becoming `sell_quote_error`. It is the same kind of fault as the non-object crash fixed after run 2, on a field FR-14 requires ("route labels"). The cases EC-07 names explicitly are met (Probe Q3: main and reference 400, 404, 429, non-object, missing `outAmount`; Probe Q4: non-JSON 200 -> outage, gap, backoff; test_phase2.py:362-367: `price_unknown`, no request).

---

## INTERFACE FLAGS (Data & Interfaces, outside the FR/EC count)

### DRIFT — D&I `sell_quotes`: pre-A3 rows
**Spec says (A4):**
> Rows saved before A3 have NULL `measured_impact` and are re-quoted, not decided on.

**What was found:**
solpaper/filters.py:162-167
```python
        # Rows saved before amendment A3 have no measured impact: re-quote rather
        # than decide on them (they'd otherwise fail every cycle until they expire).
        r = self.conn.execute(
            "SELECT * FROM sell_quotes WHERE mint = ? AND checked_at > ? "
            "AND (ok = 0 OR measured_impact IS NOT NULL) ORDER BY id DESC LIMIT 1",
```
The pre-A3 build (`0fb0637`, jupiter.py) wrote `ok = 0` rows with reason `quote_error:incomplete reply ('priceImpactPct')` whenever `priceImpactPct` was missing. After the upgrade, such a row is still read from the cache.
Probe Q1: a pre-A3 `ok = 0` row inside `sell_quote_ttl_s` (`ref_usd_in` NULL, `measured_impact` NULL) -> `sell_quote_error`, "Jupiter: quote_error:incomplete reply ('priceImpactPct')", Jupiter calls 0.
Probe Q1b: a pre-A3 `ok = 1` row -> re-quoted (2 calls), `pass`.

**The foul:** Only `ok = 1` pre-A3 rows are re-quoted. `ok = 0` pre-A3 rows are still decided on for up to 180 s, including the missing-`priceImpactPct` case that FR-14a and EC-07 (A4) now decide on measured impact. Pre-A3 rows can be told apart: the current build always writes `ref_usd_in` (filters.py:186), and the migration leaves it NULL on old rows.

---

## CLEAN PASSES

| ID    | Requirement | Verified by |
|-------|-------------|-------------|
| FR-01 | `filter` scanner job; no network for tier-1 decisions | scanner.py:137-139; filters.py:267-272; config.yaml:67; test_phase2.py:170-173, 427-432 |
| FR-02 | No liquidity -> `liquidity_unknown`, `dex=` in detail | filters.py:75-78; test_phase2.py:121-125 |
| FR-03 | Liquidity floor, both numbers in detail | filters.py:79-81; test_phase2.py:128-131 |
| FR-04 | `too_new` / `too_old` / `age_unknown` | filters.py:57-67; test_phase2.py:134-141 |
| FR-05 | Only `unchanged = 0`; `stale_data` | filters.py:68-74, 117-124; test_phase2.py:144-148 |
| FR-06 | `getMultipleAccounts` jsonParsed, batches of 100; `mint_authority_active` | solana_rpc.py:12, 76-80; filters.py:91-93; test_phase2.py:178-188 |
| FR-07 | `freeze_authority_active` | filters.py:94-96; test_phase2.py:178-182, 191-194 |
| FR-09 | `lp_lock = not_checked` on every pass | filters.py:241; test_phase2.py:294 |
| FR-10 | Logged on change; one pass row per streak | filters.py:246-264; test_phase2.py:386-394 |
| FR-11 | Per-mint cache; revoked results permanent; others re-read after TTL | filters.py:127-147; test_phase2.py:257-285; Probe Q9 (clean mint, RPC calls stay 1 after 10x TTL) |
| FR-12 | Filters section with current reasons, count and %; one-line quote summary with count, routes found, average impact, average sell price vs pool | stats.py:62-82; test_phase2.py:406-417, 469-475; Probe Q7 |
| FR-13 | Thresholds in `config.yaml` `filters:`; no literals in filters.py | config.yaml:76-92; test_phase2.py:420-424 |
| FR-14 | GET `/swap/v1/quote`, `sell_check_usd` into the pair's quote token, `no_sell_route`, `sell_quotes` row, per-mint TTL cache. The threshold sentence is superseded by FR-14a (A4). | jupiter.py:54-74, 88-133; filters.py:159-206; test_phase2.py:290-305, 370-381 |
| FR-14a | Measured impact from two back-to-back quotes; zero-payout and rounds-to-zero rules; skip above `max_sell_impact`; `priceImpactPct` cross-check, NULL when absent; `impact_disagrees` above tolerance; a failed reference follows the main-quote rules | jupiter.py:88-133; filters.py:200-205; test_phase2.py:315-343, 456-460; Probes Q3, Q5 (main at t+0 s, reference at t+5 s, for each pair), Q6a/b/c |
| EC-01 | RPC failure, timeout or JSON-RPC error in 200 -> `check_unavailable:token`, gap, never a pass | client.py:127, 162-197; filters.py:234-237; test_phase2.py:223-248 |
| EC-02 | Missing or non-mint account -> `mint_unreadable` | solana_rpc.py:51-59; filters.py:88-90; test_phase2.py:251-254 |
| EC-03 | Skip -> pass: new pass row, skip history kept | filters.py:246-264; test_phase2.py:397-403 |
| EC-04 | Pass -> fail: new skips row | filters.py:246-259; no test; Probe Q8: `pass -> liquidity_below_floor`, skips 1, passes 1 |
| EC-05 | One DEX label in rules, stats and export | config.py:22-24; filters.py:52, 232; stats.py:33-38, 95; test_phase2.py:156-167 |
| EC-06 | Non-SOL/USDC quote -> `quote_not_allowed` | filters.py:54-56; config.yaml:81-83; test_phase2.py:151-153 |

Technical constraints checked, no flag: reuses `client.Http`; RPC allow-list unchanged (client.py:33, 120-121; client.py is not in HEAD's diff); test_phase1.py and scanner.py unchanged since `180502e`; `TokenSafety` and `SellRoute` interfaces in providers/base.py.

---

## GHOST CODE

| File | Lines | No requirement found |
|------|-------|----------------------|
| solpaper/stats.py | 76, 81-82 | `Jupiter's figure disagreed: N` on the sell-quote summary line. FR-12 lists count, routes found, average impact and average sell price vs pool. FR-14a backs the `impact_disagrees` column, not this stats field. Run 2 raised it inside the FR-12 DRIFT; HEAD neither removed it nor added it to FR-12. |

---

## RECORD NOTES (no ruling)
- **EC-07 wording after A4.** The row now says "A 200 whose body isn't JSON counts as an outage (gap + backoff)" and, two sentences later, still says "Only 429/5xx/network errors count as `check_unavailable:sell_route` and open a data gap." The build follows the newer, more specific sentence (Probe Q4: `check_unavailable:sell_route`, 1 gap, backoff on). EC-07 was ruled on that reading. The "Only" sentence wasn't updated.
- **A4 adopted build behaviour into the spec.** The zero-payout rules and the rounds-to-zero rule were GHOST items in run 2. A4 adds them to FR-14a and logs them as spec errors. The tracking-limit exception cites user approval dated 2026-11-07, and the Bible records it (PROJECT_BIBLE.md:51). The A4 log row cites no user approval for the two FR-14a rules, which decide pass/skip outcomes. This is recorded, not ruled on: the referee does not judge business logic.
- **Test-harness figure.** In Probe Q7 the stats line shows `avg sell price vs pool -99.00%`. This comes from the test fake: its payout rate (`Net.rate` 1e-4) doesn't match the seeded `price_native` (1e-5). It isn't a code fault, but `test_fr12_stats_keep_sell_price_vs_pool` checks only that the label is printed, not the value.
- **FR-11 inference (carried over).** A result is cached for good only when both authorities are revoked and none of the watched extensions is present (filters.py:137-145). This still relies on Token-2022 extensions being fixed when a mint is created, which the spec doesn't state.
- **Spec freeze.** HEAD changes SPEC.md in the FR-14, FR-14a, EC-07, Technical Constraints and D&I rows and adds the A4 log row. Every change is marked in-line as A4, and the log row says it followed run 2. No unmarked edits were found.

---

## VERDICT
FLAGS RAISED

Resolve all flags before this feature is considered complete.
Update SPEC.md if a flag represents a legitimate spec change — do not
silently fix code to pass a flag that reveals a wrong requirement.

---

Notify Project Bible:
```
Flagging report for solpaper-phase2-filters (run 3): 2 flags raised (PARTIAL FR-08, PARTIAL EC-07), 1 interface flag (pre-A3 ok=0 quote rows still decided on), 1 ghost item. All run-2 flags resolved except FR-08 (narrowed); the pre-A3 crash is resolved.
Spec: specs/solpaper-phase2-filters/SPEC.md
Report: specs/solpaper-phase2-filters/FLAG-REPORT-2026-11-07-run3.md
```

Notify Anti-Vibe:
```
Ghost code detected in solpaper-phase2-filters — see FLAG-REPORT-2026-11-07-run3.md GHOST section.
```

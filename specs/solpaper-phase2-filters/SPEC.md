# SPEC: solpaper Phase 2: basic filters + skip-reason stats
**Feature slug:** solpaper-phase2-filters
**Date:** 2026-11-07
**Status:** AWAITING APPROVAL
**Requirement source:** Build prompt, "ENGINE DETAILS > Basic filter" and "BUILD ORDER 2"; user decision 2026-11-07: option 3 (skip coins without a liquidity figure for now).

---

## What This Is
Phase 2 decides, for every pair the scanner has seen, whether it is eligible
for trading rules, and logs a specific reason for every rejection. Checks run
cheapest first. The free checks run on data already in `pairs_seen`; only pairs
that pass them get the Solana RPC token checks. `python -m solpaper.stats` then
shows how many pairs were skipped for each reason.

## What This Is NOT
- No wash-trade filter, no supply-concentration check, no trade records (Phase 3).
- No trading rules, signals, sizing or paper trades (Phase 4).
- No bonding-curve liquidity reads (user chose option 3; revisit after Phase 3).
- No transaction building or simulation of any kind, signed or not.

---

## Functional Requirements

| ID | Requirement | Acceptance Criterion |
|----|-------------|----------------------|
| FR-01 | Filters run in the scanner loop as a new job (`filter`, interval in config) over every tracked pair. They work only from stored observations and cached token checks. | Test: a scanner pass with the filter job writes decisions for tracked pairs and makes no network request for pairs already decided in tier 1. |
| FR-02 | **Launchpad skip (option 3):** a pair whose latest fresh reading has no liquidity figure is skipped with reason `liquidity_unknown`, and its DEX is recorded in the detail. | Test: pump.fun pair with `liquidity_usd` NULL -> skip `liquidity_unknown`, detail includes `dex=pump-fun`. |
| FR-03 | **Liquidity floor:** skip when `liquidity_usd < liquidity_floor_usd` (config). | Test: $4,000 vs floor $10,000 -> `liquidity_below_floor`, detail shows both numbers. |
| FR-04 | **Pair age window:** skip when the pair is younger than `min_age_min` (`too_new`) or older than `max_age_hours` (`too_old`), from the provider's `pair_created_at`. A missing creation time is a skip (`age_unknown`), not a pass. | Tests for each of the three reasons. |
| FR-05 | **Stale data:** decisions use only readings with `unchanged = 0`. If the newest such reading is older than `max_reading_age_s`, skip with `stale_data`. | Test: last new reading 10 min old, limit 120s -> `stale_data`. |
| FR-06 | **Mint authority revoked:** for tier-1 passers, read the base mint via Solana RPC (`getMultipleAccounts`, `jsonParsed`, batched, up to 100 per call). Skip if `mintAuthority` is set (`mint_authority_active`). | Test with a recorded RPC response. |
| FR-07 | **Freeze authority revoked:** same read. Skip if `freezeAuthority` is set (`freeze_authority_active`). | Test with a recorded RPC response. |
| FR-08 | **Token-2022 risk extensions** (the "sell-safety where data allows" check we can do read-only): skip if the mint has any of `transferHook`, `permanentDelegate`, `nonTransferable`, `pausableConfig`, a `defaultAccountState` of frozen, or a `transferFeeConfig` with a non-zero fee (`token_extension_risk:<name>`). | Tests per extension, using recorded mint data. |
| FR-09 | **Liquidity locked or burned:** record `lp_lock = not_checked` on every pass decision, because no current provider supplies it. It never passes or fails a pair. | Test: pass decision carries `lp_lock=not_checked`. |
| FR-10 | Every decision is logged. A `skips` row is written when a pair's outcome or reason **changes** (not every cycle), with stage, reason code, human-readable detail and the metrics used. A pass is written to `filter_passes` once per pass streak. | Test: three cycles with the same skip reason -> 1 row; reason changes -> 2nd row. |
| FR-11 | Token checks are cached in `token_checks` per mint. A revoked authority is cached for good (it can't be re-enabled). Any other result is re-checked after `token_check_ttl_s`. | Test: second evaluation within TTL makes no RPC request. |
| FR-12 | Skip-reason stats: `python -m solpaper.stats` adds a "Filters" section showing pairs passed and skipped, and each pair's **current** reason with count and %. | Test on a seeded DB. |
| FR-13 | All thresholds live in `config.yaml` under `filters:`. | Code review: no threshold literals in `filters.py`. |

---

## Edge Cases & Error States

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-01 | RPC fails, rate-limits or times out for a tier-1 passer | Pair is **not** passed. It is skipped with `check_unavailable:<check>` and retried next cycle. Never assumed safe. The gap is logged by the existing client. |
| EC-02 | Mint account not found or not a token mint | Skip `mint_unreadable`. |
| EC-03 | Pair moves from skip to pass, e.g. liquidity grows past the floor | New `filter_passes` row. The earlier skip rows stay as history. |
| EC-04 | Pair passes, then later fails, e.g. liquidity collapses | New `skips` row with the new reason. Phase 4's forced exit handles any open position. |
| EC-05 | Same coin appears under two DEX labels (`pump-fun` / `pumpfun`) | DEX names are normalized to one label before any rule or stat uses them. |
| EC-06 | Quote token is another memecoin (e.g. `X / SPCX`, `sink / PUMP`) | Skip `quote_not_allowed` (allowed: SOL, USDC by default). |

---

## Out of Scope
- LP lock/burn verification: no data source yet. RugCheck or GoPlus could
  supply it, but each is a new provider (see Open Questions).
- A sell-route check through a DEX aggregator quote: a new provider (see Open Questions).
- Re-checking a pair's token after it passes, beyond the TTL rule.

---

## Technical Constraints
- Must reuse `client.Http` (budget, backoff, gap logging, cache). The RPC call
  is a read-only JSON-RPC POST. Only the methods `getMultipleAccounts` and
  `getAccountInfo` may be sent: an allow-list, enforced by test. The
  no-signing guard test is updated to still forbid every other write-style call.
- Must follow existing patterns: `None` means unknown, never 0; a provider
  failure returns `None` and is logged as a gap.
- Must not modify Phase 1 scanner behavior except adding the `filter` job.

---

## Data & Interfaces

New tables:
- `skips(id, pair_address, ts, stage, reason, detail, metrics_json)`. Stage is `tier1` or `token`.
- `filter_passes(id, pair_address, ts, metrics_json)`.
- `token_checks(mint, checked_at, mint_authority, freeze_authority, program, extensions_json, ok, reason)`.

New provider interface `TokenSafety.check(mints) -> dict[mint, TokenCheck] | None`,
implemented by `providers/solana_rpc.py`.

Config (`filters:`): `liquidity_floor_usd`, `min_age_min`, `max_age_hours`,
`max_reading_age_s`, `allowed_quotes`, `risky_extensions`, `token_check_ttl_s`,
`interval_s`.

---

## Inferred Decisions
- **Defaults:** liquidity floor **$10,000**; age window **2 min to 6 h**; max reading age **120 s**.
  From your run: 54 of 377 coins had at least $10k known liquidity.
- **Quote-token rule (EC-06)** isn't in the build prompt. It was added because
  your data has pairs quoted in other memecoins, whose USD values depend on a
  second volatile coin.
- **Token-2022 extension checks (FR-08)** are the read-only part of
  "sell-simulation where data allows". They catch tokens built to block or tax
  selling; they can't catch every honeypot.
- **Skip logging on change only (FR-10)** keeps the table readable. Per-cycle
  rows would be about 100 per minute and mostly duplicates.

---

## Open Questions
1. **LP lock/burn and honeypot data:** add one free token-risk provider (RugCheck
   or GoPlus), which needs its host allowed and its limits verified, or leave
   `not_checked` until Phase 3? Default if unanswered: leave `not_checked`.
2. **Sell-route check:** add a read-only aggregator quote (e.g. Jupiter) to
   confirm a sell route exists and measure price impact at our size? That would
   also feed Phase 4's fill model. Default if unanswered: defer to Phase 4.

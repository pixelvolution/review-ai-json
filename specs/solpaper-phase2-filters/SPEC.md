# SPEC: solpaper Phase 2: basic filters + skip-reason stats
**Feature slug:** solpaper-phase2-filters
**Date:** 2026-11-07
**Status:** APPROVED 2026-11-07, with one amendment before the build: the sell-route check (FR-14, EC-07) was added at the user's request ("Do sell route"). Open Question 1 took its default (LP lock `not_checked`).
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
| FR-12 | Skip-reason stats: `python -m solpaper.stats` adds a "Filters" section showing pairs passed and skipped, and each pair's **current** reason with count and %, plus a one-line summary of sell quotes (count, routes found, average impact, average sell price vs pool). *(A2: the quote summary was added.)* | Test on a seeded DB. |
| FR-13 | All thresholds live in `config.yaml` under `filters:`. | Code review: no threshold literals in `filters.py`. |
| FR-14 | **Sell-route check** (amendment): for pairs that pass tier 1 and the token checks, ask Jupiter's read-only quote API (`GET /swap/v1/quote`) to sell `sell_check_usd` worth of the base token into the pair's quote token. Skip `no_sell_route` when Jupiter answers that the token isn't tradable or has no route. Skip `sell_impact_too_high` when `priceImpactPct` exceeds `max_sell_impact`. Every quote is stored in `sell_quotes`: amount in, amount out, price impact, route labels, and the effective sell price vs the pool price. Quotes are cached per mint for `sell_quote_ttl_s`. | Tests with recorded quote and no-route responses. |

---

## Edge Cases & Error States

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-01 | RPC fails, rate-limits or times out for a tier-1 passer | Pair is **not** passed. It is skipped with `check_unavailable:<check>` and retried next cycle. Never assumed safe. The gap is logged by the existing client. |
| EC-02 | Mint account not found or not a token mint | Skip `mint_unreadable`. |
| EC-03 | Pair moves from skip to pass, e.g. liquidity grows past the floor | New `filter_passes` row. The earlier skip rows stay as history. |
| EC-04 | Pair passes, then later fails, e.g. liquidity collapses | New `skips` row with the new reason. Phase 4's forced exit handles any open position. |
| EC-05 | Same coin appears under two DEX labels (`pump-fun` / `pumpfun`) | DEX names are normalized to one label before any rule or stat uses them. |
| EC-07 | Jupiter's reply to a sell quote is anything other than a complete quote | *(Amended A2.)* HTTP 400 is Jupiter saying it can't be sold: skip `no_sell_route`. Any other 4xx, or a 200 missing `outAmount`/`priceImpactPct`, is a skip with `sell_quote_error`; a missing value is never read as 0. No price or decimals to size the quote: skip `price_unknown`, and no request is sent. Only 429/5xx/network errors count as `check_unavailable:sell_route` and open a data gap. |
| EC-06 | Quote token is another memecoin (e.g. `X / SPCX`, `sink / PUMP`) | Skip `quote_not_allowed` (allowed: SOL, USDC by default). |

---

## Out of Scope
- LP lock/burn verification: no data source yet. RugCheck or GoPlus could
  supply it, but each is a new provider (see Open Questions).
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
  *(A2 note: log and `http_error` event text now name the full URL instead of the
  path, because RPC calls have no path. Message text only; behavior is unchanged.)*

---

## Data & Interfaces

New tables:
- `skips(id, pair_address, ts, stage, reason, detail, metrics)`. Stage is `tier1`, `token` or `sell_route`. *(A2: column `metrics`, stage `sell_route`.)*
- `filter_passes(id, pair_address, ts, metrics)`. Each new pass also writes a `filter_pass` event for the live feed. *(A2)*
- `token_checks(mint, checked_at, readable, program, decimals, mint_authority, freeze_authority, extensions)`. *(A2: `readable` and `decimals` replace `ok`/`reason`. The decision is derived from the facts at evaluation time, and decimals are needed to size the sell quote.)*
- `sell_quotes(id, pair_address, mint, ts, checked_at, usd_in, amount_in, out_mint, out_amount, price_impact, effective_vs_pool, routes, ok, reason)`. `reason` is `no_route:<code>` or `quote_error:<what>`. *(A2: `checked_at` added for the TTL.)*
- `filter_state(pair_address, outcome, reason, updated_at)`: current outcome, used to log changes only.

New provider interfaces in `providers/base.py`: `TokenSafety.check(mints) -> dict[mint, TokenCheck] | None`,
implemented by `providers/solana_rpc.py`; and `SellRoute.quote_sell(...)`, implemented
by `providers/jupiter.py`. *(A2: the `SellRoute` interface was added.)*

Config (`filters:`): `liquidity_floor_usd`, `min_age_min`, `max_age_hours`,
`max_reading_age_s`, `allowed_quotes`, `risky_extensions`, `token_check_ttl_s`,
`sell_check_usd`, `sell_quote_slippage_bps`, `max_sell_impact`, `sell_quote_ttl_s`.
*(A2: the job interval lives with the other jobs at `scanner.intervals_s.filter`.
The DEX label map is a top-level `dex_aliases` key, because stats and export use it too.)*

Providers (limits verified 2026-11-07):
- Solana RPC (`getMultipleAccounts`, up to 100 mints per call). The response headers report method/RPS limits.
- Jupiter quote API (`lite-api.jup.ag`, keyless). The response headers report 5 requests per ~10 s window, counted per IP.

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
2. ~~Sell-route check~~: resolved, added as FR-14.
3. **Finding during the build:** Jupiter also quotes pump.fun bonding-curve coins
   (route label `Pump.fun`). A sell quote could stand in for the missing
   liquidity figure and make those coins eligible. Not built; this needs a
   decision after Phase 3.

---

## Amendment Log

| ID | Date | When | Change | Why |
|----|------|------|--------|-----|
| A1 | 2026-11-07 | Before the build | Added FR-14 (sell-route check) and EC-07 | User request: "Do sell route". |
| A1b | 2026-11-07 | **During the build** | Added the `filter_state` table, the provider-limit notes, and Open Question 3 | Recorded honestly: these were added while building, not before. The flag report caught it. No FR or EC wording changed. |
| A2 | 2026-11-07 | After FLAG-REPORT-2026-11-07 (**spec errors**) | EC-07 rewritten to cover every non-quote reply; Data & Interfaces corrected (column names, `sell_route` stage, `SellRoute` interface, config key locations, `sell_quote_slippage_bps`); FR-12 and the `filter_pass` event documented | The report showed the spec was silent or wrong on these. Code flags (FR-08, FR-11, FR-14, EC-01, EC-05, the missing `TokenSafety` interface, hard-coded literals) were fixed in code, not by editing the spec. A new flag run follows. |

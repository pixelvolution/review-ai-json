# SPEC: solpaper Phase 3: research phase
**Feature slug:** solpaper-phase3-research
**Date:** 2026-11-07
**Status:** APPROVED. The user said "start phase 3 and then phase 4. With tests in between." The trade schema (TRADE_SCHEMA.md) is reported with the phase rather than as a separate stop, at the user's request.
**Requirement source:** build prompt, "RESEARCH PHASE" and "Wash-trade filter".

## What This Is
Trade-level records for a sample of new pairs; a wash-trade filter in the engine
(run before any trading rule sees a pair); supply concentration as a separate
check; and a research report that replays rule variations with the honest fill
model, per activity and concentration bucket, with multiple-testing records.

## What This Is NOT
No live trading or wallet (Phase 4; the fill model built here is shared). No
funding-source clustering (data unavailable without an RPC key; recorded as such).

## Functional Requirements
| ID | Requirement | Acceptance |
|----|-------------|-----------|
| P3-01 | `TradeRecords` provider (GeckoTerminal trades) behind the provider interface, with its own budget bucket, cached, returning None on failure. Rows stored in `trade_records` by trade id. | Tests with the recorded fixture, 429 and outage. |
| P3-02 | Trade schema documented before use. | `TRADE_SCHEMA.md`. |
| P3-03 | Wash metrics from trades at or before an `as_of` time: trades, unique wallets, trades per wallet, round-trip wallet share (same wallet buys and sells within `round_trip_s`), top-3 wallet volume share, size regularity (CV of USD sizes), timing regularity (CV of inter-trade gaps). Funding clusters = NULL with a reason. | Tests on built trade sets, incl. a future trade being ignored. |
| P3-04 | Wash score in [0, 1] = weighted mean of component scores (weights and scales in config). Score >= `threshold` is flagged; reason + metrics stored in `wash_checks`. Too few trades = `insufficient_trades`, never a pass. | Tests. |
| P3-05 | Supply concentration: top-10 holder share of supply via read-only RPC (`getTokenLargestAccounts`, `getTokenSupply`; owners via `getMultipleAccounts`), also shown excluding accounts owned by the pair itself. Stored in `holder_checks`; refusal = NULL + reason. Not an engine filter by default. | Tests with recorded-shape responses and a refusal. |
| P3-06 | Engine: a `wash` filter stage after the token stage and before the sell-route stage. At most `wash_checks_per_pass` new trade fetches per pass (no record first, then oldest); reuse within `wash_max_age_s`; otherwise `wash_check_pending`. Flagged = skip `wash_score_high` (detail lists the components). | Tests. |
| P3-07 | `python -m solpaper.research`: seeded random sample of recent pool launches (liquidity known), recorded; fetch trades and holders within budget; bucket by 24 h trade count (<100, 100-999, >=1000) and top-10 share (<30%, 30-60%, >=60%, unknown); replay every rule variation on stored observations with the fill model; report per bucket and rule: pairs, share net-positive after costs, median net, wash-flagged pairs that would have looked profitable on price alone. Groups under `min_pairs` are marked "insufficient data / likely noise". Every rule variation tried is stored in `research_hypotheses`. Sample size and window first. | Tests on seeded data. |
| P3-08 | `fills` module (shared with Phase 4): an entry or exit decided at time t fills at the first observation at or after t + `delay_s`, at that observation's price, with constant-product impact against its liquidity, plus fee, flat tip and a deterministic honeypot draw (total loss on sale). Exits: take-profit, stop-loss, time stop, liquidity collapse. No observation before the fill time is used for a fill. | Tests: delay, impact math, fees, honeypot, no look-ahead, a drained pool pays ~0. |

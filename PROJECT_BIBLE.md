# Project Bible

> Living knowledge base, maintained by the Bible agent (`.agents/bible.md`). Do not edit manually; invoke the Bible agent instead.

## Architecture Decisions

| Date | Decision | Rationale | Author |
|------|----------|-----------|--------|
| 2026-11-07 | Mock wallet is a **SQLite ledger only**: money as integer micro-dollars; charges round up, proceeds round down | Exact arithmetic, no float drift; rounding always costs us, never flatters. No keys, no addresses, no network. | cornelas / Claude |
| 2026-11-07 | **Cooked runs are frozen by SQLite triggers**, not just by code | Rejects any later insert/update/delete on a cooked run, and any delete of history, even from a hand-typed SQL session. Only `reset.py` starts run N+1. | Claude |
| 2026-11-07 | On cook, **open positions are liquidated at the last mark** with full exit costs | Leaves the cooked record final instead of holding positions forever. | Claude |
| 2026-11-07 | **Cup rule skims only after a close, limited to cash on hand**; peak equity is lowered by the skim | Unrealized gains can't push money into the vault. A skim is a transfer, not a loss, so it can't trip the kill switch. | Claude |
| 2026-11-07 | **Suggestions live in a separate `control.db`**; the engine never opens it | Makes "the engine must never read the suggestions table" structural, and two tests enforce it (source scan + database-open spy). | Claude |
| 2026-11-07 | Multiple-testing margin = `min_margin + noise_sigma * sqrt(2 ln N)` over every test ever run | sqrt(2 ln N) is roughly how far the best of N noise results drifts above zero, so each extra idea raises the bar. | Claude |
| 2026-11-07 | `solpaper/` is a **new package**, built phase by phase per the build prompt | `mock_wallet/` was a prototype for a different data source (Binance candles); the build prompt needs a live scanner for new Solana pairs. The wallet and validation logic will be carried into Phase 4 and Phase 6. | cornelas |
| 2026-11-07 | **Discovery: GeckoTerminal `new_pools` + trending; DexScreener boosts + profiles. Tier-1 quotes: DexScreener batch pairs (30/call), GeckoTerminal for pairs DexScreener lacks** | DexScreener has no "newest pairs" endpoint, and brand-new pools are often missing from it for their first minutes. | Claude |
| 2026-11-07 | **Request budget = even pacing at a safety share of the published limit**, exponential backoff on 429/5xx/network errors, every outage logged in `data_gaps` | The scheduler slows before any cap instead of bursting into it; nothing is fabricated during a gap. | Claude |
| 2026-11-07 | **Cache keeps the latest response per request key**; the normalized history lives in `pairs_seen` | Keeping every raw response would be over 1 GB/day at the current polling rate. | Claude |
| 2026-11-07 | **Cloud reporting target: OPEN** (Cloudflare was named; Project Uploader runs on Bunny) | See `specs/cloud-reporting/CLARIFY.md`. Nothing is built until it's answered. | - |

## Completed Features

| Date | Feature | Summary | Related Files |
|------|---------|---------|---------------|
| 2026-11-07 | Mock wallet | Ledger with fees, slippage, tips, failed-sell total loss; equity-based sizing; cooked state + `reset.py`; vault cup rule; snapshots; dashboard + morning report. 22 tests. | `mock_wallet/` |
| 2026-11-07 | Strategy research | 152 configs x 4 cost profiles on 2 years of Binance memecoin candles, train/test split. None profitable at the wallet's default costs; the one CEX-cost survivor failed robustness checks. | `mock_wallet/research/FINDINGS.md` |
| 2026-11-07 | Suggestion + validation pipeline (prototype) | MCP `suggest_change`, replay + holdout validation, multiple-testing guard, shadow mode, logged config changes. 40 tests. | `mock_wallet/validate_suggestion.py`, `mcp_server.py`, `control.py` |
| 2026-11-07 | solpaper Phase 1: scanner, logger, cache, request budget | Read-only discovery and re-quoting of new Solana pairs into `pairs`/`pairs_seen`; per-provider pacing, backoff and gap logging; timestamped response cache; signing guard test. 11 tests. | `solpaper/` |

## Incidents & Resolutions

| Date | Incident | Root Cause | Resolution | Prevention |
|------|----------|------------|------------|------------|
| 2026-11-07 | Wallet crashed on prices passed as numpy numbers | `Decimal(repr(np.float64))` parses `"np.float64(0.5)"`, which isn't a number | `D()` converts with `repr(float(value))` | Regression test `test_accepts_numpy_prices` |
| 2026-11-07 | MCP suggestions written to the wrong database | MCP stdio clients start the server with a minimal environment, so `MOCK_WALLET_*` variables weren't inherited | Pass `env` in the client config | Documented in `mock_wallet/README.md` |
| 2026-11-07 | Scanner recorded cached responses as new observations | The pair-quote cache window (30s) equalled the refresh interval (30s) | Snapshots carry `from_cache` and are skipped; cache window 25s | Test asserts a cached response adds no rows |
| 2026-11-07 | Scanner logged `refresh_tracked 0 snaps` about one cycle in three (seen on the first local Windows run) | Each job's next run was timed from the start of the loop pass, so a job late in the pass came back ~20s after it actually ran, inside its own 25s cache window. The cached result was correctly not re-recorded, so no data was wrong, but a cycle was skipped. | Each job is now timed from its own start | Test `test_each_job_is_timed_from_its_own_start` |
| 2026-11-07 | 56% of DexScreener re-quotes were identical to the previous reading (1,573 of them on pairs that had traded in the last 5 min), and missing CDN ages were stored as `0` | DexScreener's numbers update less often than our 30s poll, and its CDN caches 30s. The client treated "no Age header" as "fresh". | Unknown age is now `NULL`; each observation has an `unchanged` flag (same provider, same numbers); migration adds the column to existing DBs | Tests for unknown age, repeat flag and old-DB migration. Later phases must use only `unchanged = 0` rows as new price prints and treat data age as a lower bound. |
| 2026-11-07 | GeckoTerminal 429 on the first call of each run | Keyless access is rate-limited per IP, shared with everyone on the same IP | Backoff + gap logging work as designed | Consider one free Demo key (100 calls/min) |

## Environment Notes

| Date | Change | Details |
|------|--------|---------|
| 2026-11-07 | Cloud dev environment `review-ai-json (market data)` (personal) | Allowed hosts: `api.geckoterminal.com`, `data-api.binance.vision`, `api.dexscreener.com`, `api.mainnet-beta.solana.com`, `docs.dexscreener.com`, `apiguide.geckoterminal.com`, `solana.com`, `docs.coingecko.com`. |
| 2026-11-07 | Provider limits (verified) | DexScreener: 60/min for profiles/boosts, 300/min for pairs/tokens, 30 addresses per call (docs). GeckoTerminal keyless: IP-based and shared, no number published; Demo key 100/min (CoinGecko docs). Solana RPC: 150/method, 250 RPS, 40 connections (live response headers; docs page behind a bot check). |
| 2026-11-07 | Data quirk | DexScreener reports no `liquidity` for pump.fun bonding-curve pairs, which are most new pairs. Liquidity filters must treat it as unknown, never 0. |
| 2026-11-07 | Branch | All work on `ccr-41c3942e-sk6vga`. |

## Conventions & Patterns

- **Missing is `None`, never `0`.** Provider parsers return `None` for absent numbers; filters must decide what unknown means.
- **A provider failure returns `None`** and is logged as a gap. Callers never substitute a value.
- **Money is integer micro-dollars** in SQLite and `Decimal` in Python.
- **Secrets never enter git**: `.secrets/`, `secrets.json`, `*.env`, `*.key` are gitignored.
- **solpaper uses its own infrastructure.** It never touches another project's database, storage or edge script.

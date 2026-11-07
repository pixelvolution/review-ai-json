# solpaper

**This is a simulator.** It watches new Solana token pairs and paper-trades
them with fake money to find out what a night of trading would really look
like after costs.

- It never holds keys, connects a wallet, signs transactions or places real orders.
  There is no code path that could, and a test enforces this.
- A profitable paper result does **not** mean real profits. Real fills are
  worse, real rugs are faster, and a strategy that worked last night can fail tonight.
- "This doesn't work" is a valid, useful result.

## Status

Built in phases, each reviewed before the next.

| Phase | What | State |
|---|---|---|
| 1 | Scanner, logger, cache, request budget | done |
| 2 | Basic filters, skip-reason stats | next |
| 3 | Research: trade records, wash filter, buckets, replay | |
| 4 | Mock wallet, paper executor, risk | |
| 5 | MCP server, dashboard | |
| 6 | Suggestion and validation pipeline | |
| 7 | Morning report, systemd units, full README | |

## Phase 1: run the scanner

    pip install -r solpaper/requirements.txt
    python -m solpaper.scanner --minutes 30     # or no flag to run until stopped
    python -m solpaper.stats                     # what it captured
    python -m solpaper.export                    # CSVs of readings, filter decisions and study
                                                 # findings into .\exports, with a README.txt
    python -m pytest solpaper/tests

Config: `solpaper/config.yaml`. Database: `solpaper/solpaper.db`, or set `SOLPAPER_DB`.

### Data providers (verified 2026-11-07)

| Provider | Used for | Published limit | Our budget |
|---|---|---|---|
| GeckoTerminal (keyless) | Newest pools, trending pools, quotes for pairs DexScreener lacks | IP-based, shared with everyone on the same IP, no number published | 10/min, backoff on 429 |
| DexScreener | Boosted and new-profile tokens; batch pair quotes, 30 per call | 60/min for profiles/boosts; 300/min for pairs/tokens | 50% of each |
| Solana public RPC (phase 2) | Mint/freeze authority, holders | Docs behind a bot check; the RPC's own headers report 150/method, 250 RPS, 40 connections | 60/min |

Rules the client follows:
- Every response is cached with a timestamp. The same request is never sent twice within its freshness window.
- Requests are spaced evenly, so the scanner slows down before any cap.
- A 429, a 5xx or a network error pauses that provider with exponential backoff, and the outage is logged in `data_gaps`. Nothing is fabricated to fill it.
- One keyless client per provider: no multiple accounts, no IP rotation, no proxies.

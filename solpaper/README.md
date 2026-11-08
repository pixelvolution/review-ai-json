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
| 2 | Basic filters, skip-reason stats | done |
| 3 | Research: trade records, wash filter, buckets, replay | done |
| 4 | Mock wallet, paper executor, risk | done |
| 5 | MCP server, dashboard | |
| 6 | Suggestion and validation pipeline | |
| 7 | Morning report, systemd units, full README | |

## The easy way: the portal (Windows)

One window in your browser to start and stop everything, with the mock
wallet, open positions, trades, signals, filters, study progress and logs.

1. Double-click **`Setup solpaper.bat`** once. It creates `.venv` and installs
   the packages. Skip this if you already have a `.venv` in the repo folder.
2. Double-click **`Start solpaper.bat`**. It starts everything in the
   background and opens the portal at http://127.0.0.1:8765. Stop everything
   with **Quit** in the portal. Closing the browser tab doesn't stop it.
3. Optional: double-click **`build_exe.bat`** once to get `solpaper.exe`.
   It does exactly what `Start solpaper.bat` does. Right-click it and choose
   *Send to > Desktop (create shortcut)* for a desktop icon.

Buttons:

- **Update**: runs `git pull` (fast-forward only), installs packages if
  `requirements.txt` changed, lists what's new and offers to restart. The
  restart brings back whichever of the scanner and study were running, and the
  page reloads by itself. Needs `git` on PATH; otherwise pull in GitHub
  Desktop and press Update to restart.
- **Start new run** (only shown when the wallet is cooked): starts the next
  $100 run, same as `python reset.py`. The cooked run stays in the records as
  it ended. The scanner restarts so it trades with the new wallet. The Paper
  trades panel counts the current run only.

The .exe is only a launcher: it runs the code in this folder with `.venv`, so a
`git pull` updates the app without rebuilding. The exe must stay in the repo folder.

Notes:

- The portal only listens on 127.0.0.1, so other machines can't reach it. Its
  buttons need a token that changes every launch, so other websites can't press them.
- Running a second copy doesn't start a second scanner; it opens the running one.
- Logs: `logs\scanner.log`, `logs\study.log`, `logs\portal.log`.
- **Export CSVs** writes the same files as `python -m solpaper.export`.
- When the portal is started by the .exe, Stop may have to force-close the
  scanner rather than let it finish its step (no console to send Ctrl+Break
  to). SQLite keeps the database consistent either way; at most the step in
  progress is lost.

## Fast reads for open positions

DexScreener's numbers only refresh about every 30 seconds, so on its own the
paper trader sees a stop-loss up to 30 s late and fills entries 15-60 s after
deciding. For the pairs it holds, the scanner now reads **pumpswap** pools
straight from the chain every 5 seconds (read-only Solana RPC: the pool's two
reserve accounts) and runs the paper trader right after, in its own thread.

- Pumpswap only: other pools (Raydium, Meteora, launchpads) aren't priced by
  their reserve ratio, so they keep the 30 s readings. `pool_reads.csv` says
  which pool is read how, and why.
- A pool's first on-chain price must agree with DexScreener's before it's used.
- Reads go to `position_marks`, not the main readings table, so filters,
  research and the study are unchanged.
- Open positions show **P&L if sold now**: after price impact, the exit fee
  and tip, against the cost basis (which includes the entry fee and tip).

## Commands

    python -m solpaper.portal --autostart   # the portal (what the .bat and .exe run)
    python -m solpaper.scanner              # scanner + filters + paper trading (until stopped)
    python -m solpaper.study run            # Launch Outcome Study, in a second window
    python -m solpaper.stats                # capture, filters, paper-trading summary
    python -m solpaper.research             # phase 3 research report (fetches trade records)
    python -m solpaper.export               # CSVs + README.txt into .\exports
    python reset.py                         # start a new mock-wallet run after a cook

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

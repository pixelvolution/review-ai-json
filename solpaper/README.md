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
2. Double-click **`Start solpaper.bat`**. The portal opens at
   http://127.0.0.1:8765 and starts the scanner and the study. Keep the black
   window open: closing it, or pressing **Quit** in the portal, stops everything.
3. Optional, for a real `.exe`: double-click **`build_exe.bat`** once. It
   builds `solpaper.exe` into the repo folder (PyInstaller, about a minute).
   Double-clicking `solpaper.exe` starts everything with no black window, or
   just reopens the page if it's already running. Use **Quit** in the portal
   to stop it. Right-click the .exe and choose *Send to > Desktop (create
   shortcut)* for a desktop icon.

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

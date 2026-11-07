# Launch Outcome Study: Step 0 integration plan

**Date:** 2026-11-07
**Status:** AWAITING GO-AHEAD. No study code until approved.
**Requirement source:** "Add-on Feature: Launch Outcome Study" (user message, 2026-11-07).

## 1. What exists today vs what the study wants to reuse

| Study needs | Status in `solpaper/` | Consequence |
|---|---|---|
| Scanner (pairs, `pairs_seen`) | Built (Phase 1) | Reused as is. The study reads what the scanner records. |
| Provider interface, cache, request budget | Built (Phases 1-2) | Reused (`client.Http`, `providers/*`). |
| SQLite schema conventions | Built | Followed: `None` = unknown, append-only tables, migrations. |
| Wash filter | **Not built** (main Phase 3) | The study's wash score waits for it. |
| Honest fill model (delay, impact, fees, tip, honeypot) | **Not built** (main Phase 4) | The late-buyer simulation waits for it. It should be one shared module that both use. |
| MCP server | **Not in solpaper** (prototype in `mock_wallet/`) | Study tools are added when the solpaper MCP server is built (main Phase 5). |
| Dashboard, morning report | **Not in solpaper** (main Phases 5 and 7) | The Study page and section come with them. |
| `suggest_change` + validation pipeline | Prototype in `mock_wallet/`; solpaper Phase 6 | Study patterns go through it once it exists. |

So the study can't be built in its six phases on its own. Its phases 2-3 can
start now, but 4-6 depend on main Phases 3-7.

## 2. How it plugs in

- **Separate process: `python -m solpaper.study`.** It shares the SQLite
  database (WAL mode), the HTTP cache and the `Http` client, but has its own
  scheduler. The main scanner keeps running unchanged, so the study can't slow
  the engine's loop (we saw a 35-second filter pass delay a refresh once).
- **Separate budget buckets.** Each provider gets a `study` bucket in
  `config.yaml`. Engine plus study stays under each provider's published
  limit, so the study can never starve the engine.
- **One-way dependency.** The study may import engine modules; nothing in the
  engine imports the study. A test enforces it, the same way the prototype
  enforces that the engine never reads the suggestions table.
- **Cohort = every pair the scanner records.** Two sources, as you asked:
  `pumpfun_curve` (still on a bonding curve) and `pool` (regular pools).
  Graduation = the same mint later appearing in a regular pool.
- **Cheap features from data we already have.** At first_seen + 5/15/60 min,
  the study freezes features computed only from `pairs_seen` rows with
  `observed_at <= first_seen + W`. The loader takes an `as_of` cutoff and
  can't return later rows, the same pattern as the prototype's replay.
- **Wallet-level features** for a random sample per hour plus every graduate,
  from new read-only providers (section 3). The sample is drawn before any
  filter result is known, and the draw and the rate are recorded.
- **Checkpoint queue in SQLite** (`study_queue`), batched and paced. A
  missed checkpoint is recorded as a gap; a token whose data can't be fetched
  is labeled `unknown`. `dead` needs positive evidence: liquidity and trading
  observed collapsed. A provider no longer returning the pair is not evidence.
- **Tables:** as you listed, plus `study_queue` and `study_gaps`. Features
  and outcomes live in separate tables, with SQLite triggers making
  `launch_features` append-only and immutable, like the mock wallet's frozen runs.

## 3. Data reality (probed 2026-11-07, read-only)

| Feature | Source | Works today? |
|---|---|---|
| Liquidity, volume, price, buys/sells, age | Scanner (`pairs_seen`) | Yes, free |
| Bonding-curve progress, graduation | Solana RPC `getAccountInfo` on the pump.fun pair address (it **is** the curve account, owner `6EF8...F6P`, 166 bytes) | Yes. Byte layout to be verified before use. Likely also holds the creator wallet. |
| Unique wallets, trades per wallet, buy/sell ratio, launch-block bundles | GeckoTerminal `/pools/{pool}/trades`: wallet, kind, block number, USD size | Yes, keyless. Capped at the last 300 trades in 24 h, so it must be fetched early for busy tokens. |
| Top-holder concentration | Solana RPC `getTokenLargestAccounts` | **No:** the public RPC answered "429 too many requests for this call" on the first try |
| Creator prior launches, death rate, funding source | Solana RPC `getSignaturesForAddress` / `getTransaction` | **No:** same 429 |
| Socials / links | DexScreener `info` (only coins with a paid or claimed profile, which biases it); GeckoTerminal token info | Partly. Source to be chosen in study phase 2. |
| Peak multiple between checkpoints | Our own observations (12 h tracking), then checkpoints only | Underestimates peaks after 12 h. GeckoTerminal OHLCV could fill it for sampled tokens. |

**Coverage problem.** Every GeckoTerminal `new_pools` poll in the last run
returned 20 of 20 never-seen pools. We read one page per minute, so we see at
most about 20 launches a minute and miss the rest. The cohort is a capped
sample with an unknown coverage rate, and the spec requires recording the
sampling rate.

## 4. Decisions needed

1. **Build order.** Recommended interleaving:
   - A. Finish main Phase 2 (this week's work).
   - B. Study 2: schema, cohort, cheap features and curve reads, running live.
   - C. Main Phase 3: trade-record schema (shown to you), then the wash filter.
   - D. Study 2b: wallet-level features plus sampling; Study 3: checkpoints and labels.
   - E. Main Phase 4: mock wallet and the shared honest fill model; Study 4: late-buyer simulation.
   - F. Study 5: `study_report.py`.
   - G. Main Phases 5-7, including the study's MCP tools, dashboard page and morning-report section.
2. **Separate study process with its own budget share** (recommended), or run
   inside the scanner loop.
3. **One free GeckoTerminal / CoinGecko demo key** (recommended). It gives a
   dedicated 100 requests a minute, so we can read several `new_pools` pages,
   measure the true launch rate and record coverage. Without it, the study has
   to say its coverage is unknown.
4. **One free RPC provider key** (e.g. a free tier from a Solana RPC provider)
   for holders and creator history on sampled tokens. That's one account, within
   your rules. Without it, those three features are dropped or replaced with
   proxies from the trade feed (e.g. top-wallet share of volume).
5. **Peak multiple:** own observations plus checkpoints, labeled "observed peak"
   (recommended to start), or add GeckoTerminal OHLCV for sampled tokens.

## 5. Scale estimate

At about 20 launches a minute (the current cap), roughly 29k launches a day
would each need 5 checkpoints, about 144k lookups a day. At 30 per DexScreener
call that's about 3 calls a minute, comfortably inside budget. The curve reads
batch 100 per RPC call. Storage is roughly 100-200 MB a day, dominated by
`pairs_seen`. Tracking 1,000 pairs at a time already uses about 68
DexScreener calls a minute of the engine's 150.

## 6. Tests planned (from your list)

Features never use data after first_seen + window (a test plants future rows
and asserts they're ignored); unfetchable data is labeled `unknown`, never
`dead`; sampling is random, recorded and independent of filter results; the
late-buyer simulation applies delay, slippage, fees and honeypot losses with
no future prices; the checkpoint queue survives a restart; no signing or
sending path (the guard test is extended to `solpaper/study/`); a pattern that
wins in only one time window is flagged as noise; insufficient samples are
never reported as findings. Plus: the engine never imports the study.

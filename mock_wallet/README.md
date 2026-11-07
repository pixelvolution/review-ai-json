# Mock Wallet

Paper money with real consequences. A SQLite ledger that a paper-trading loop
debits on entry and credits on exit. It holds no keys and no addresses, and makes no network calls.

## Use

Run everything from inside `mock_wallet/`.

```python
from wallet import MockWallet, RunCooked
from trader import PaperTrader

trader = PaperTrader(MockWallet())          # run 1 is created on first use ($100)
try:
    pos = trader.buy("BONK", quote_price)               # sized from current equity
    trader.mark({"BONK": latest_price})                 # may trip the kill switch
    trader.sell(pos["position_id"], exit_price)         # or failed=True
except RunCooked as e:
    log.critical(e)                                     # stop the bot
    raise SystemExit(1)
```

| Command | What it does |
|---|---|
| `python reset.py` | Start run N+1 with a fresh balance. Refuses while a run is alive. |
| `python reset.py --force` | Cook the alive run (liquidate at last marks), then start N+1. |
| `python reset.py --start-balance 250` | Any config field can be set per run (see `--help`). |
| `python report.py` | Morning report (markdown). `--json` for machine-readable output. |
| `uvicorn dashboard:app --port 8050` | Read-only dashboard; `/api/status`, `/api/runs`. |
| `python -m pytest` | Tests. |

The database is `mock_wallet/mock_wallet.db`. Override it with `MOCK_WALLET_DB`.

## Config

Each value is set by the `MOCK_WALLET_<NAME>` env var or a `reset.py` flag. A
run's config is stored on its row, so changing env vars never changes the rules of a live run.

| Field | Default | Meaning |
|---|---|---|
| `start_balance` | 100 | Starting cash |
| `min_trade_size` | 5 | Minimum viable trade. Equity below this cooks the run. |
| `max_drawdown` | 0.25 | Drawdown from peak equity that cooks the run |
| `vault_target_multiple` | 2 | Cup rule fires when equity exceeds this × start |
| `position_fraction` | 0.10 | Share of equity per trade |
| `fee_rate` | 0.01 | Charged on entry notional and on exit gross |
| `slippage_rate` | 0.01 | Buys fill above the quote, sells below it |
| `tip` | 0.05 | Flat priority tip per transaction |
| `charge_tip_on_failed_sell` | true | A failed sell still burns its tip |

## Rules and how they're enforced

- **Money is exact.** Money is stored as integer micro-dollars and handled as `Decimal`. Charges round up and proceeds round down.
- **No overdraft.** `notional + fee + tip` must fit in the balance or the trade is refused. Orders are never silently shrunk. A `CHECK (balance >= 0)` backs this up in the database.
- **Costs are what real money costs.** The entry debit is notional + fee + tip. Slippage is taken in the fill price, so you get fewer tokens on a buy and less cash on a sell. A failed sell credits nothing, zeroes the position and burns the tip.
- **Sizing compounds.** `size = max(equity × position_fraction, min_trade_size)`, capped at what the balance can afford. Equity is cash plus marked open positions. The vault is not equity.
- **COOKED.** The run cooks when drawdown from peak reaches `max_drawdown`, or equity drops below `min_trade_size`. This is checked after every open, close and mark. Open positions are liquidated at their last mark with full exit costs, the cause is logged at CRITICAL and stored, and the run is frozen.
  - SQLite triggers reject any later insert, update or delete on a cooked run.
  - Runs, positions, the ledger, events and snapshots can never be deleted.
  - Nothing restarts a run except `reset.py`.
- **Cup rule.** After a close, if equity exceeds the target, the excess above start capital moves from cash to the vault, up to the cash on hand. Each skim is logged as an event.
  - The vault is never sized against or spendable.
  - A trigger stops the vault from ever decreasing.
  - Peak equity drops by the amount skimmed, so a skim never trips the kill switch.
- **Snapshots.** One is written after every open and every close, plus at run start and at cooking. Each records balance, vault, open-position value, equity, peak and drawdown. The run's worst drawdown is tracked on each mark as well.

## Tables

`runs` (one per life, current state), `positions`, `ledger` (every balance
movement; its sum always equals the balance), `events` (opens, closes, failed
sells, skims, kills), `snapshots`.

## Suggestions and validation

Ideas for parameter changes come in through one MCP tool, get tested on paper
money, and wait for you. Nothing is ever applied automatically.

```
suggest_change ──> suggestions (pending) ──> validate_suggestion.py ──> validated ──> you: set_config.py
     (MCP)            control.db              replay A + holdout B      dashboard        (logged)
```

| File | Role |
|---|---|
| `mcp_server.py` | MCP server with one tool, `suggest_change(parameter, proposed_value, rationale)`. It writes a `pending` row and nothing else. |
| `validate_suggestion.py` | Tests pending suggestions. Updates status only. |
| `shadow.py` | Optional forward test on live data, run daily. |
| `set_config.py` | The only way the live config changes. Every change is logged with a reason. |
| `live_config.json` | The strategy and parameters the engine reads. |
| `validation.json` | Windows, thresholds and fill model. These can't be changed by a suggestion. |
| `market.py` | The `pairs_seen` price history that replays read. `python market.py import-binance` seeds it. |
| `control.db` | Suggestions, validation evidence and the config change log. Never opened by trading code. |

Register the MCP server with your client. For Claude Code:

    claude mcp add mock-wallet -- python /path/to/mock_wallet/mcp_server.py

MCP clients start servers with a minimal environment. If you moved any
database with `MOCK_WALLET_*` variables, set them in the client config too.

### How a suggestion is tested

Each test compares two runs: the live config unchanged (the baseline) and the
live config with the suggested change (the candidate).

1. **Replay, window A.** Both run over the window the suggestion was derived from (default `research_window`).
2. **Holdout, window B.** The same on `holdout_window`. It must not overlap A, or the test refuses to run.

Each run uses the honest fill model:
- a signal fills `fill_delay_bars` bars after it fires;
- the wallet charges real slippage, fees and tips;
- `honeypot_rate` of entries can't be sold. Honeypots are seeded per pair and entry time, so the baseline and the candidate hit the same ones;
- no bar at or after the window end is ever loaded.

Each run gets its own in-memory `MockWallet`.

| Verdict | When |
|---|---|
| `insufficient data` | Any run has fewer than `min_trades` (50) trades, or a window has no data. It never passes. |
| `validated` | Beats the baseline net of costs by the required margin on **both** windows, with drawdown no worse. |
| `likely noise` | Wins only one of the two windows. |
| `rejected` | Wins neither window. |

**Multiple-testing guard.** Every test ever run is counted, re-tests included.
The required margin is `min_margin + noise_sigma × √(2·ln N)`, roughly how far
the best of N pure-noise results drifts above zero. So each extra idea tried
raises the bar for the next.

**Shadow mode** (`"shadow": {"enabled": true, "days": 14}`). A suggestion that
passes goes to `shadow` instead of `validated`. `shadow.py` then replays the
candidate and the live config side by side, each in its own mock wallet, on
bars the bot records after the shadow starts. When the period ends it becomes
`validated`, `rejected` or `insufficient data`.

**Applying.** Validated suggestions show on the dashboard and in the morning
report with their evidence: trades tested, net result against the baseline on
each window, drawdown, and the tests-tried count at the time. Apply one with
`python set_config.py --suggestion ID --reason "..."`. Hand edits to
`live_config.json` are detected and logged by `set_config.py --check`, by the
validator and by the morning report.

**Isolation.** The suggestions table is in its own file, and trading code
never opens it. Tests confirm this by scanning the engine's source and by
recording every database file the engine opens.

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

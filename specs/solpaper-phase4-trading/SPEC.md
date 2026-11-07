# SPEC: solpaper Phase 4: mock wallet, paper executor, risk
**Feature slug:** solpaper-phase4-trading
**Date:** 2026-11-07
**Status:** APPROVED. The user said "start phase 3 and then phase 4. With tests in between."
**Requirement source:** build prompt, "Sizer", "Paper executor", "Risk", "MOCK WALLET".

| ID | Requirement | Acceptance |
|----|-------------|-----------|
| P4-01 | MockWallet in solpaper (ported prototype, `wallet_` tables): ledger only, never negative, a trade never larger than the balance, cooked state frozen by triggers, `python reset.py` starts run N+1, cup-rule vault never traded, snapshot after every open/close. | Tests: overdraw, vault, cooked frozen, reset. |
| P4-02 | Sizer: position = `position_fraction` x current equity (min `min_trade_size`, capped by what the balance affords); `max_open_positions`; `daily_loss_cap_usd` on realized P&L per UTC day. Refusals logged in `skips` (stage `sizer`). | Tests. |
| P4-03 | Entries only for pairs whose current filter decision is `pass`; one trade per pair (config). Each entry and exit decision is a `signals` row with a reason. | Tests. |
| P4-04 | Fills via `fills`: first observation at or after decision + `delay_s`, within `max_fill_wait_s`, else a gap (entry: `entry_failed`; exit: retried). Entry price = mid x notional / CP value after impact; exit proceeds = CP value of the position against exit liquidity. Fee and tip charged by the wallet. | Tests: delay, ignored earlier prints, impact math, debits, gap. |
| P4-05 | Exits: take-profit, stop-loss, time stop, liquidity collapse (shared `exit_trigger`). Honeypot (deterministic draw at entry) = failed sell, total loss, tip paid. | Tests. |
| P4-06 | Risk: the wallet's kill switch (25% drawdown from peak) or minimum equity cooks the run; the executor halts all trading and logs `trading_halted` once. | Test. |
| P4-07 | `trades` records every trade with reason, delays, mid and effective prices, impact, fees, tips, proceeds, P&L; stats and export show them. | Tests + export group `trading`. |

**Inferred decisions:** the executor runs inside the scanner loop as the `trade`
job (10 s). The scanner keeps observing after a cook, so the research data has no
gaps. The default exit rule (TP 50%, SL 25%, 60 min, liquidity -50%) is a
starting point, not a recommendation; Phase 3 research and Phase 6 validation
decide changes.

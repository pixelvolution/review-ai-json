# Strategy research: findings (2026-11-07)

**Bottom line:** none of 152 strategy configurations across 20 families made
money reliably after realistic costs. At the wallet's default costs, none made
money in either year.

## Setup
- 16 Binance-listed memecoins (DOGE, SHIB, PEPE, FLOKI, BONK, WIF, BOME, MEME,
  PEOPLE, 1000SATS, NEIRO, TURBO, PNUT, ACT, TRUMP, PENGU), 1h candles,
  Nov 2024 to Nov 2026.
- Settings picked on year 1 (train), judged on year 2 (test, unseen).
- Every fill goes through `MockWallet`: 10% of equity per trade, cup rule on.
  Signals fill at the next bar's open; stops are checked pessimistically.
- Four cost profiles: none, CEX (0.1% fee, 0.05% slip), lean DEX (0.3%, 0.3%,
  $0.01 tip), and the wallet defaults (1%, 1%, $0.05 tip).

## Results
| Costs | Profitable year 1 | Profitable year 2 | Both |
|---|---|---|---|
| None | 25 | 24 | 6 |
| CEX 0.1% | 9 | 11 | 2 |
| Lean DEX 0.3% | 3 | 1 | 1 (2 trades, meaningless) |
| Wallet defaults | 0 | 0 | 0 |

- The market: all 16 memecoins fell in both years (equal-weight −72%, then
  −55%). Long-only memecoin trading swam against that the whole time.
- Costs decide everything. At the defaults a $10 round trip costs ~5%;
  random entries lose ~95% in a year.
- Even with zero costs, only 6 of 25 year-1 winners stayed profitable in
  year 2. That's about what luck alone produces.
- The one survivor at CEX costs, MACD (scale 4) with a BTC trend filter, made +11%
  then +6%, but:
  - 27 of 30 neighbouring settings lost money in year 2, so the winner is
    most likely a lucky point;
  - on BTC/ETH/SOL it lost money;
  - at lean-DEX costs it lost 42%;
  - with the 25% kill switch it was cooked in year 1;
  - block-bootstrap odds of ending a year ahead: about 52%, a coin flip.
- Mean reversion (RSI or Bollinger dip buying) did well in year 2 but lost in
  year 1. Its results depend on market conditions, so it isn't a reliable edge.

## Not covered
- Fresh on-chain launches (sniping, copy-trading wallets): this needs on-chain
  data, including tokens that rugged, which wasn't available here. These
  strategies compete with professional MEV bots on speed.
- Shorting (the wallet is long-only).

## Reproduce
    python data.py && python evaluate.py && python summarize.py && python robustness.py

# Trade-record schema (GeckoTerminal, keyless)

**Source:** `GET https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/trades`
**Recorded:** 2026-11-07 (fixture: `solpaper/tests/fixtures/gt_trades.json`)
**Limits:** at most the last **300 trades from the past 24 hours** per call. Keyless
rate limit is per IP, shared with other users, no number published.

## Structure (parent -> child)

```
response
└── data: [ trade, ... ]           newest first; no paging without a paid plan
    └── trade
        ├── id          "solana_<block>_<tx_hash>_<index>_<unix>": unique per swap (a tx can hold several)
        ├── type        "trade"
        └── attributes
            ├── block_number            int     Solana slot
            ├── block_timestamp         ISO-8601 UTC ("...Z")
            ├── tx_hash                 base58 transaction signature
            ├── tx_from_address         base58 wallet that signed (the "trader"; may be a bot/router)
            ├── kind                    "buy" | "sell" (relative to the pool's base token)
            ├── from_token_address      mint paid in
            ├── to_token_address        mint received
            ├── from_token_amount       decimal string, whole tokens
            ├── to_token_amount         decimal string, whole tokens
            ├── price_from_in_usd       decimal string
            ├── price_to_in_usd         decimal string
            ├── price_from_in_currency_token  decimal string
            ├── price_to_in_currency_token    decimal string
            ├── volume_in_usd           decimal string, the trade's USD size
            └── is_anomalous            bool (GeckoTerminal's own outlier flag)
```

No `meta` or paging cursor comes back on the keyless endpoint.

## What we store (`trade_records`)

One row per trade `id`: pair, block, timestamp, tx hash, wallet, kind, USD
volume, base-token price in USD (the `price_*_in_usd` of the base side) and base
amount, plus `fetched_at`. Missing values stay NULL, never 0.

## What it can't tell us

- **Funding sources** (who funded each wallet): needs wallet transaction
  history (RPC `getSignaturesForAddress`), which the public RPC refuses. The wash
  filter's funding-cluster component is recorded as unavailable.
- **Full history for busy pools:** 300 trades may cover only minutes. The
  trade-count buckets use DexScreener's 24 h buy/sell counters instead.

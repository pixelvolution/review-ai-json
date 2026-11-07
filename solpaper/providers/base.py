"""Provider interfaces. Every data source sits behind one of these so it can
be swapped without touching the scanner.

Return conventions:
  * a list (possibly empty) when the provider answered;
  * None when it couldn't (rate limit, outage, bad data). The scanner logs
    the gap and moves on. It never invents data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class PairSnapshot:
    pair_address: str
    source: str
    dex: str | None = None
    base_mint: str | None = None
    quote_mint: str | None = None
    base_symbol: str | None = None
    name: str | None = None
    pair_created_at: str | None = None   # ISO-8601 UTC
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    mcap_usd: float | None = None
    vol: dict = field(default_factory=dict)      # m5/h1/h6/h24 -> USD
    buys: dict = field(default_factory=dict)     # m5/h1/h24 -> count
    sells: dict = field(default_factory=dict)
    change: dict = field(default_factory=dict)   # m5/h1/h24 -> percent
    data_age_s: float | None = None
    from_cache: bool = False   # served from our cache: already recorded once


class PairDiscovery(Protocol):
    """Finds pairs worth looking at: new pools, trending, promoted."""
    name: str

    def discover(self, job: str) -> list[PairSnapshot] | list[str] | None: ...


class PairQuotes(Protocol):
    """Cheap summary data for known pair addresses (tier 1)."""
    name: str
    max_batch: int

    def pairs(self, addresses: list[str]) -> list[PairSnapshot] | None: ...


@dataclass
class TradeRecord:
    trade_id: str
    pair_address: str
    t: float                     # unix seconds (block time)
    block_number: int | None
    tx_hash: str | None
    wallet: str | None
    kind: str | None             # buy | sell
    volume_usd: float | None
    price_usd: float | None      # base token price in USD at this trade
    base_amount: float | None


class TradeRecords(Protocol):
    """Trade-level records for a pool (tier 2: only for pairs that need them)."""
    name: str

    def trades(self, pair_address: str, base_mint: str | None) -> list[TradeRecord] | None: ...


class TokenSafety(Protocol):
    """Reads token mint facts (authorities, extensions) for safety filters."""
    name: str

    def check(self, mints: list[str]) -> dict | None: ...


class SellRoute(Protocol):
    """Quotes a sale without trading: can this amount be sold, and at what impact?"""
    name: str

    def quote_sell(self, mint: str, decimals: int, out_mint: str, usd_amount: float,
                   price_usd: float, price_native: float | None, slippage_bps: int,
                   ref_usd: float, disagree_tol: float): ...


def f(x) -> float | None:
    """Parse a provider number; None stays None (never 0)."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def i(x) -> int | None:
    v = f(x)
    return None if v is None else int(v)

"""Sell-route check via Jupiter's keyless quote API (read-only GET).

Asks "if we sold this many tokens right now, what would we get?" It only
fetches a quote: no transaction is built, signed or sent.

Limit, from Jupiter's response headers (2026-11-07): 5 requests per ~10 s
window, counted per IP.

``priceImpactPct`` is a fraction (0.02 = 2%). Verified 2026-11-07: a $10 BONK
sale returned 0.1114 SOL (~$10) with 0.00065, and a near-worthless sale
returning 17 lamports reported 0.99997.
"""

from __future__ import annotations

from dataclasses import dataclass

QUOTE_DECIMALS = {
    "So11111111111111111111111111111111111111112": 9,   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
}


@dataclass
class SellQuote:
    ok: bool                      # a route exists
    reason: str | None = None     # error code when no route
    amount_in: int | None = None  # base units of the token sold
    out_amount: int | None = None
    price_impact: float | None = None   # fraction, e.g. 0.02 = 2%
    effective_vs_pool: float | None = None  # sell price vs pool price, -0.03 = 3% worse
    routes: str | None = None


class Jupiter:
    name = "jupiter"

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg

    def quote_sell(self, mint: str, decimals: int, out_mint: str, usd_amount: float,
                   price_usd: float, price_native: float | None) -> SellQuote | None:
        """Quote selling ``usd_amount`` of ``mint`` into ``out_mint``.

        None means the check couldn't run (rate limit, outage). A SellQuote
        with ok=False means Jupiter answered: there is no way to sell.
        """
        if not price_usd or price_usd <= 0 or decimals is None:
            return None
        amount = int(usd_amount / price_usd * 10 ** decimals)
        if amount <= 0:
            return None
        data = self.http.get_json(
            self.name, "quote", "/swap/v1/quote",
            {"inputMint": mint, "outputMint": out_mint, "amount": amount, "slippageBps": 100},
            answer_statuses=frozenset({400}))
        if data is None:
            return None
        if data.get("_http_status") == 400:
            return SellQuote(ok=False, reason=data.get("errorCode") or "NO_ROUTE",
                             amount_in=amount)
        try:
            out_amount = int(data["outAmount"])
            impact = float(data.get("priceImpactPct") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        effective = None
        out_dec = QUOTE_DECIMALS.get(out_mint)
        if price_native and out_dec is not None:
            got = out_amount / 10 ** out_dec
            expected = amount / 10 ** decimals * price_native
            effective = got / expected - 1 if expected else None
        routes = ",".join(dict.fromkeys(
            r.get("swapInfo", {}).get("label", "?") for r in data.get("routePlan") or []))
        return SellQuote(ok=True, amount_in=amount, out_amount=out_amount, price_impact=impact,
                         effective_vs_pool=effective, routes=routes)

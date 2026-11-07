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

# Client errors are answers about our request, not outages (429 is a rate limit).
CLIENT_ERRORS = frozenset(set(range(400, 500)) - {429})

QUOTE_DECIMALS = {
    "So11111111111111111111111111111111111111112": 9,   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
}


@dataclass
class SellQuote:
    ok: bool                      # a route exists and the reply was complete
    reason: str | None = None     # "no_route:<code>" or "quote_error:<what>" when not ok
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
                   price_usd: float, price_native: float | None,
                   slippage_bps: int) -> SellQuote | None:
        """Quote selling ``usd_amount`` of ``mint`` into ``out_mint``.

        Returns None only when the check couldn't run (429, 5xx, network).
        Otherwise a SellQuote: ok, no_route (HTTP 400: Jupiter says it can't be
        sold) or quote_error (any other 4xx, or an incomplete reply). The caller
        must have a positive price and known decimals.
        """
        amount = int(usd_amount / price_usd * 10 ** decimals)
        if amount <= 0:
            return SellQuote(ok=False, reason="quote_error:amount rounds to zero")
        data = self.http.get_json(
            self.name, "quote", "/swap/v1/quote",
            {"inputMint": mint, "outputMint": out_mint, "amount": amount,
             "slippageBps": slippage_bps},
            answer_statuses=CLIENT_ERRORS)
        if data is None:
            return None
        status = data.get("_http_status")
        if status == 400:
            return SellQuote(ok=False, reason=f"no_route:{data.get('errorCode') or 'NO_ROUTE'}",
                             amount_in=amount)
        if status is not None:
            return SellQuote(ok=False, reason=f"quote_error:HTTP {status}", amount_in=amount)
        try:
            out_amount = int(data["outAmount"])
            impact = float(data["priceImpactPct"])
        except (KeyError, TypeError, ValueError) as e:
            # Unknown impact is not zero impact.
            return SellQuote(ok=False, reason=f"quote_error:incomplete reply ({e})",
                             amount_in=amount)
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

"""Sell-route check via Jupiter's keyless quote API (read-only GET).

Asks "if we sold this many tokens right now, what would we get?" It only
fetches a quote: no transaction is built, signed or sent.

Limit, from Jupiter's response headers (2026-11-07): 5 requests per ~10 s
window, counted per IP.

``priceImpactPct`` is a fraction (0.02 = 2%). Verified 2026-11-07: a $10 BONK
sale returned 0.1114 SOL (~$10) with 0.00065, and a near-worthless sale
returning 17 lamports reported 0.99997.

But that field is unreliable on some Pump.fun AMM quotes: a live run saw it
report 1.0 (100%) while the quoted payout was within ~1% of the pool price. So
the decision uses a *measured* impact (spec amendment A3): two quotes at the
same moment, a tiny reference sale and our real size, compared by payout per
token. Jupiter's own figure is kept as a cross-check.
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
    ok: bool                      # a route exists and both quotes were complete
    reason: str | None = None     # "no_route:<code>" or "quote_error:<what>" when not ok
    amount_in: int | None = None  # base units of the token sold (our size)
    out_amount: int | None = None
    ref_amount_in: int | None = None   # the tiny reference sale
    ref_out_amount: int | None = None
    measured_impact: float | None = None  # 1 - payout rate at our size / reference rate
    price_impact: float | None = None     # Jupiter-reported (cross-check only); None if absent
    impact_disagrees: bool = False        # reported vs measured differ by > tolerance
    effective_vs_pool: float | None = None  # payout vs DexScreener price (may be stale)
    routes: str | None = None


class Jupiter:
    name = "jupiter"

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg

    def _one(self, mint, out_mint, amount, slippage_bps):
        """One quote. None = couldn't run; else (reason|None, data)."""
        data = self.http.get_json(
            self.name, "quote", "/swap/v1/quote",
            {"inputMint": mint, "outputMint": out_mint, "amount": amount,
             "slippageBps": slippage_bps},
            answer_statuses=CLIENT_ERRORS)
        if data is None:
            return None
        status = data.get("_http_status")
        if status == 400:
            return f"no_route:{data.get('errorCode') or 'NO_ROUTE'}", data
        if status is not None:
            return f"quote_error:HTTP {status}", data
        try:
            int(data["outAmount"])
        except (KeyError, TypeError, ValueError) as e:
            return f"quote_error:incomplete reply ({e})", data
        return None, data

    def quote_sell(self, mint: str, decimals: int, out_mint: str, usd_amount: float,
                   price_usd: float, price_native: float | None, slippage_bps: int,
                   ref_usd: float, disagree_tol: float) -> SellQuote | None:
        """Quote selling ``usd_amount`` of ``mint`` into ``out_mint``, plus a
        ``ref_usd`` reference sale, and measure the price impact from payouts.

        Returns None only when a quote couldn't run (429, 5xx, network).
        Otherwise a SellQuote: ok, no_route (HTTP 400: Jupiter says it can't be
        sold) or quote_error (any other 4xx, an incomplete reply, or an impact
        that can't be measured). The caller must have a positive price and
        known decimals.
        """
        amount = int(usd_amount / price_usd * 10 ** decimals)
        ref_amount = int(ref_usd / price_usd * 10 ** decimals)
        if amount <= 0 or ref_amount <= 0:
            return SellQuote(ok=False, reason="quote_error:amount rounds to zero")
        main = self._one(mint, out_mint, amount, slippage_bps)
        if main is None:
            return None
        reason, data = main
        if reason:
            return SellQuote(ok=False, reason=reason, amount_in=amount)
        ref = self._one(mint, out_mint, ref_amount, slippage_bps)
        if ref is None:
            return None
        ref_reason, ref_data = ref
        if ref_reason:
            return SellQuote(ok=False, reason=f"quote_error:reference {ref_reason}",
                             amount_in=amount)
        out_amount, ref_out = int(data["outAmount"]), int(ref_data["outAmount"])
        if ref_out > 0:
            measured = 1 - (out_amount / amount) / (ref_out / ref_amount)
        elif out_amount == 0:
            measured = 1.0  # neither sale pays anything: worthless
        else:
            return SellQuote(ok=False, reason="quote_error:reference sale paid 0",
                             amount_in=amount)
        try:
            impact = float(data["priceImpactPct"])
        except (KeyError, TypeError, ValueError):
            impact = None  # cross-check only; unknown stays unknown, never 0
        effective = None
        out_dec = QUOTE_DECIMALS.get(out_mint)
        if price_native and out_dec is not None:
            got = out_amount / 10 ** out_dec
            expected = amount / 10 ** decimals * price_native
            effective = got / expected - 1 if expected else None
        routes = ",".join(dict.fromkeys(
            r.get("swapInfo", {}).get("label", "?") for r in data.get("routePlan") or []))
        return SellQuote(ok=True, amount_in=amount, out_amount=out_amount,
                         ref_amount_in=ref_amount, ref_out_amount=ref_out,
                         measured_impact=measured, price_impact=impact,
                         impact_disagrees=impact is not None
                         and abs(impact - measured) > disagree_tol,
                         effective_vs_pool=effective, routes=routes)

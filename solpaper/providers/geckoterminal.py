"""GeckoTerminal public (keyless) API: new pools, trending pools, pool quotes.

Keyless access is IP-rate-limited and shared with everyone on the same IP,
so the budget in config.yaml is deliberately tiny.
"""

from __future__ import annotations

from .base import PairSnapshot, f, i

NETWORK = "solana"


def _strip(gid: str | None) -> str | None:
    """'solana_ABC' -> 'ABC'."""
    return gid.split("_", 1)[1] if gid and "_" in gid else gid


def parse_pool(item: dict, source: str) -> PairSnapshot:
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return PairSnapshot(
        pair_address=a.get("address") or _strip(item.get("id")),
        source=source,
        dex=((rel.get("dex") or {}).get("data") or {}).get("id"),
        base_mint=_strip(((rel.get("base_token") or {}).get("data") or {}).get("id")),
        quote_mint=_strip(((rel.get("quote_token") or {}).get("data") or {}).get("id")),
        base_symbol=(a.get("name") or "").split(" / ")[0] or None,
        name=a.get("name"),
        pair_created_at=a.get("pool_created_at"),
        price_usd=f(a.get("base_token_price_usd")),
        price_native=f(a.get("base_token_price_native_currency")),
        liquidity_usd=f(a.get("reserve_in_usd")),
        fdv_usd=f(a.get("fdv_usd")),
        mcap_usd=f(a.get("market_cap_usd")),
        vol={k: f(vol.get(k)) for k in ("m5", "h1", "h6", "h24")},
        buys={k: i((tx.get(k) or {}).get("buys")) for k in ("m5", "h1", "h24")},
        sells={k: i((tx.get(k) or {}).get("sells")) for k in ("m5", "h1", "h24")},
        change={k: f(chg.get(k)) for k in ("m5", "h1", "h24")},
    )


class GeckoTerminal:
    name = "geckoterminal"
    max_batch = 30

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg
        self.fresh = cfg.get("freshness_s", {})

    def _pools(self, path, source, fresh_key, params=None):
        data = self.http.get_json(self.name, "keyless", path, params,
                                  fresh_s=self.fresh.get(fresh_key, 60))
        if data is None or not isinstance(data.get("data"), list):
            return None
        age, cached = self.http.last_age_s, self.http.last_from_cache
        out = []
        for item in data["data"]:
            s = parse_pool(item, source)
            s.data_age_s, s.from_cache = age, cached
            out.append(s)
        return out

    def discover(self, job: str):
        if job == "gt_new_pools":
            pages = int(self.cfg["scanner"].get("gt_new_pool_pages", 1))
            out = []
            for page in range(1, pages + 1):
                got = self._pools(f"/networks/{NETWORK}/new_pools", "gt_new_pools",
                                  "gt_new_pools", {"page": page})
                if got is None:
                    return out or None
                out.extend(got)
            return out
        if job == "gt_trending":
            return self._pools(f"/networks/{NETWORK}/trending_pools", "gt_trending",
                               "gt_trending", {"page": 1})
        raise ValueError(job)

    def pairs(self, addresses: list[str]):
        if not addresses:
            return []
        return self._pools(f"/networks/{NETWORK}/pools/multi/{','.join(addresses)}",
                           "gt_refresh", "gt_pools_multi")

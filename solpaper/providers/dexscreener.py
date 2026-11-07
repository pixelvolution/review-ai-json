"""DexScreener public API: promoted tokens (discovery) and pair quotes.

Documented limits: 60 req/min for token-profiles and token-boosts ('slow'
bucket); 300 req/min for pairs and tokens ('fast' bucket); up to 30
addresses per call.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import PairSnapshot, f, i

CHAIN = "solana"


def parse_pair(p: dict, source: str) -> PairSnapshot:
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    created = p.get("pairCreatedAt")
    return PairSnapshot(
        pair_address=p.get("pairAddress"),
        source=source,
        dex=p.get("dexId"),
        base_mint=(p.get("baseToken") or {}).get("address"),
        quote_mint=(p.get("quoteToken") or {}).get("address"),
        base_symbol=(p.get("baseToken") or {}).get("symbol"),
        name=f"{(p.get('baseToken') or {}).get('symbol')} / "
             f"{(p.get('quoteToken') or {}).get('symbol')}",
        pair_created_at=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
        if created else None,
        price_usd=f(p.get("priceUsd")),
        price_native=f(p.get("priceNative")),
        liquidity_usd=f((p.get("liquidity") or {}).get("usd")),
        fdv_usd=f(p.get("fdv")),
        mcap_usd=f(p.get("marketCap")),
        vol={k: f(vol.get(k)) for k in ("m5", "h1", "h6", "h24")},
        buys={k: i((tx.get(k) or {}).get("buys")) for k in ("m5", "h1", "h24")},
        sells={k: i((tx.get(k) or {}).get("sells")) for k in ("m5", "h1", "h24")},
        change={k: f(chg.get(k)) for k in ("m5", "h1", "h24")},
    )


class DexScreener:
    name = "dexscreener"
    max_batch = 30

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg
        self.fresh = cfg.get("freshness_s", {})

    def _tokens_from(self, path, fresh_key):
        data = self.http.get_json(self.name, "slow", path, fresh_s=self.fresh.get(fresh_key, 120))
        if not isinstance(data, list):
            return None
        return list(dict.fromkeys(d["tokenAddress"] for d in data
                                  if d.get("chainId") == CHAIN and d.get("tokenAddress")))

    def discover(self, job: str):
        """Promoted tokens -> their most liquid Solana pair."""
        if job == "ds_boosts":
            tokens = self._tokens_from("/token-boosts/latest/v1", "ds_boosts")
        elif job == "ds_profiles":
            tokens = self._tokens_from("/token-profiles/latest/v1", "ds_profiles")
        else:
            raise ValueError(job)
        if tokens is None:
            return None
        out = []
        for k in range(0, len(tokens), self.max_batch):
            batch = tokens[k:k + self.max_batch]
            data = self.http.get_json(self.name, "fast", f"/tokens/v1/{CHAIN}/{','.join(batch)}",
                                      fresh_s=self.fresh.get("ds_pairs", 30))
            if not isinstance(data, list):
                continue
            best: dict[str, dict] = {}
            for p in data:
                t = (p.get("baseToken") or {}).get("address")
                liq = f((p.get("liquidity") or {}).get("usd")) or 0
                if t and (t not in best or liq > (f((best[t].get("liquidity") or {})
                                                     .get("usd")) or 0)):
                    best[t] = p
            for p in best.values():
                s = parse_pair(p, job)
                s.data_age_s, s.from_cache = self.http.last_age_s, self.http.last_from_cache
                out.append(s)
        return out

    def pairs(self, addresses: list[str]):
        if not addresses:
            return []
        data = self.http.get_json(self.name, "fast",
                                  f"/latest/dex/pairs/{CHAIN}/{','.join(addresses)}",
                                  fresh_s=self.fresh.get("ds_pairs", 30))
        if data is None:
            return None
        out = []
        for p in data.get("pairs") or []:
            s = parse_pair(p, "ds_refresh")
            s.data_age_s, s.from_cache = self.http.last_age_s, self.http.last_from_cache
            out.append(s)
        return out

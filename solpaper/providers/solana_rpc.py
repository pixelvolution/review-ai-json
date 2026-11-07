"""Token safety checks read from the Solana public RPC (read-only).

One ``getMultipleAccounts`` call (jsonParsed) reads up to 100 mint accounts.
From each mint we take: mint authority, freeze authority, decimals, and any
Token-2022 extensions that can block, tax or claw back a sale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_BATCH = 100
TOKEN_PROGRAMS = {"spl-token", "spl-token-2022"}


@dataclass
class TokenCheck:
    mint: str
    readable: bool
    program: str | None = None
    decimals: int | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    extensions: dict = field(default_factory=dict)   # name -> state


def risky_extensions(check: TokenCheck, names: list[str]) -> list[str]:
    """Risky Token-2022 extensions on this mint (spec FR-08).

    Presence alone counts for transferHook, permanentDelegate, nonTransferable
    and pausableConfig: an unset hook program or delegate can still be set
    later by its authority. defaultAccountState counts only when it is
    "frozen", and transferFeeConfig only when a fee is non-zero.
    """
    out = []
    for name in names:
        if name not in check.extensions:
            continue
        st = check.extensions[name] or {}
        if name == "defaultAccountState" and st.get("accountState") != "frozen":
            continue
        if name == "transferFeeConfig":
            fees = [(st.get(k) or {}).get("transferFeeBasisPoints") or 0
                    for k in ("newerTransferFee", "olderTransferFee")]
            if max(fees) <= 0:
                continue
        out.append(name)
    return out


def parse_mint(mint: str, value: dict | None) -> TokenCheck:
    if not value:
        return TokenCheck(mint, readable=False)
    data = value.get("data")
    if not isinstance(data, dict) or data.get("program") not in TOKEN_PROGRAMS:
        return TokenCheck(mint, readable=False)
    parsed = data.get("parsed") or {}
    if parsed.get("type") != "mint":
        return TokenCheck(mint, readable=False)
    info = parsed.get("info") or {}
    return TokenCheck(
        mint, readable=True, program=data["program"], decimals=info.get("decimals"),
        mint_authority=info.get("mintAuthority"), freeze_authority=info.get("freezeAuthority"),
        extensions={e.get("extension"): e.get("state") for e in info.get("extensions") or []})


def holder_concentration(largest: list[dict], supply_raw: int, owners: dict[str, str | None],
                         pair_address: str, top_n: int = 10) -> dict:
    """Top-N holder share of supply, and the same excluding accounts owned by the pair."""
    amounts = [(a["address"], int(a.get("amount") or 0)) for a in largest[:top_n]]
    if not supply_raw:
        return {"top_share": None, "top_share_ex_pair": None, "largest_share": None,
                "n_accounts": len(amounts)}
    top = sum(x for _, x in amounts)
    ex = sum(x for addr, x in amounts if owners.get(addr) != pair_address)
    return {"top_share": top / supply_raw, "top_share_ex_pair": ex / supply_raw,
            "largest_share": amounts[0][1] / supply_raw if amounts else None,
            "n_accounts": len(amounts)}


class SolanaRpc:
    name = "solana_rpc"

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg

    def holders(self, mint: str, pair_address: str, bucket: str = "rpc") -> dict | None:
        """Top-10 concentration (read-only). None when the RPC refuses or fails."""
        largest = self.http.rpc_read(self.name, bucket, "getTokenLargestAccounts", [mint])
        supply = self.http.rpc_read(self.name, bucket, "getTokenSupply", [mint])
        if not largest or not isinstance(largest.get("value"), list) or not supply:
            return None
        accts = largest["value"]
        owners_res = self.http.rpc_read(self.name, bucket, "getMultipleAccounts",
                                        [[a["address"] for a in accts[:10]],
                                         {"encoding": "jsonParsed"}])
        owners = {}
        for a, v in zip(accts[:10], (owners_res or {}).get("value") or []):
            info = (((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            owners[a["address"]] = info.get("owner")
        try:
            supply_raw = int(supply["value"]["amount"])
        except (KeyError, TypeError, ValueError):
            return None
        return holder_concentration(accts, supply_raw, owners, pair_address)

    def check(self, mints: list[str]) -> dict[str, TokenCheck] | None:
        """Read mint accounts. None if any batch failed (caller retries later)."""
        out: dict[str, TokenCheck] = {}
        for k in range(0, len(mints), MAX_BATCH):
            batch = mints[k:k + MAX_BATCH]
            res = self.http.rpc_read(self.name, "rpc", "getMultipleAccounts",
                                     [batch, {"encoding": "jsonParsed"}])
            if res is None or not isinstance(res.get("value"), list):
                return None
            for mint, value in zip(batch, res["value"]):
                out[mint] = parse_mint(mint, value)
        return out

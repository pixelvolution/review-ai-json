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
    """Extensions present *and* armed. An extension slot with no authority,
    program or fee set can't be used against holders, so it isn't counted."""
    out = []
    for name in names:
        if name not in check.extensions:
            continue
        st = check.extensions[name] or {}
        if name == "transferHook" and not st.get("programId"):
            continue
        if name == "permanentDelegate" and not st.get("delegate"):
            continue
        if name == "pausableConfig" and not (st.get("authority") or st.get("paused")):
            continue
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


class SolanaRpc:
    name = "solana_rpc"

    def __init__(self, http, cfg: dict):
        self.http, self.cfg = http, cfg

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

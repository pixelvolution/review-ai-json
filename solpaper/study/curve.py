"""Read pump.fun bonding-curve accounts (read-only RPC).

On pump.fun, the pair address the providers report *is* the bonding-curve
account. Layout after the 8-byte account discriminator (little-endian u64):
virtual token reserves, virtual SOL reserves, real token reserves, real SOL
reserves, token total supply; then a one-byte ``complete`` flag.

Verified 2026-11-07 against live data:
  * curve price = virtual SOL / virtual tokens (9 and 6 decimals) matched
    DexScreener's price exactly on 9 of 11 tokens;
  * a graduated token has ``complete`` = 1 and zeroed reserves.
Not verified: real SOL doesn't match the account's SOL balance on most
tokens, and parameters differ between curve variants. So progress and the
creator field are not derived here; the raw reserves are stored for later.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

MAX_BATCH = 100
HEADER = 8           # account discriminator
FIELDS_END = HEADER + 5 * 8 + 1


@dataclass
class CurveState:
    address: str
    readable: bool
    complete: bool | None = None
    virtual_tokens: float | None = None   # whole tokens (6 decimals)
    virtual_sol: float | None = None      # SOL (9 decimals)
    real_tokens: float | None = None
    real_sol: float | None = None
    price_sol: float | None = None


def parse(address: str, value: dict | None, program: str) -> CurveState:
    if not value or value.get("owner") != program:
        return CurveState(address, readable=False)
    try:
        raw = base64.b64decode(value["data"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return CurveState(address, readable=False)
    if len(raw) < FIELDS_END:
        return CurveState(address, readable=False)
    vt, vs, rt, rs, _supply = struct.unpack_from("<5Q", raw, HEADER)
    complete = raw[HEADER + 40] == 1
    vtok, vsol = vt / 1e6, vs / 1e9
    return CurveState(address, True, complete, vtok, vsol, rt / 1e6, rs / 1e9,
                      price_sol=(vsol / vtok) if vtok else None)


def read(http, addresses: list[str], program: str) -> dict[str, CurveState] | None:
    """Batched read. None if any batch failed (the caller records a gap)."""
    out: dict[str, CurveState] = {}
    for k in range(0, len(addresses), MAX_BATCH):
        batch = addresses[k:k + MAX_BATCH]
        res = http.rpc_read("solana_rpc", "study", "getMultipleAccounts",
                            [batch, {"encoding": "base64"}])
        if res is None or not isinstance(res.get("value"), list):
            return None
        for addr, value in zip(batch, res["value"]):
            out[addr] = parse(addr, value, program)
    return out

"""Store trade records (shared by the engine's wash filter and the research runner)."""

from __future__ import annotations


def store_trades(conn, trades, fetched_at: float) -> int:
    n = 0
    for t in trades:
        n += conn.execute(
            "INSERT OR IGNORE INTO trade_records (trade_id, pair_address, t, block_number, "
            "tx_hash, wallet, kind, volume_usd, price_usd, base_amount, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.trade_id, t.pair_address, t.t, t.block_number, t.tx_hash, t.wallet, t.kind,
             t.volume_usd, t.price_usd, t.base_amount, fetched_at)).rowcount
    return n


def trades_as_of(conn, pair: str, as_of: float) -> list[dict]:
    """Only trades at or before ``as_of`` (no look-ahead)."""
    return [dict(r) for r in conn.execute(
        "SELECT t, wallet, kind, volume_usd FROM trade_records WHERE pair_address = ? AND t <= ? "
        "ORDER BY t", (pair, as_of))]

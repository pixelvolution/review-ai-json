"""Position sizing from live wallet equity.

The size is read from the wallet every time, never from a fixed ticket, so
wins compound and losses shrink the next trade.
"""

from __future__ import annotations

from decimal import Decimal

from wallet import MockWallet, q_down


def size_position(wallet: MockWallet) -> Decimal | None:
    """Notional for the next trade, or None if no viable trade fits.

    size = equity * position_fraction, raised to min_trade_size if smaller,
    then capped so notional + fee + tip fits in the available cash. The vault
    is not equity and is never sized against.
    """
    cfg = wallet.config
    if wallet.is_cooked:
        return None
    target = max(q_down(wallet.equity() * cfg.position_fraction), cfg.min_trade_size)
    size = min(target, wallet.max_affordable_notional())
    if size < cfg.min_trade_size:
        return None
    return size

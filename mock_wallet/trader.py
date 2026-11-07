"""PaperTrader: the entry point a strategy loop calls.

    trader = PaperTrader(MockWallet())
    pos = trader.buy("BONK", quote_price)          # sized from equity
    trader.mark({"BONK": latest_price})            # can trip the kill switch
    trader.sell(pos["position_id"], exit_price)    # or failed=True

Every call raises ``RunCooked`` once the run is cooked, including the call that
cooked it (the finished operation is on ``exc.result``). The loop should catch
it, log it, and exit. Nothing here restarts a run; that is ``python reset.py``.
"""

from __future__ import annotations

import logging

from sizer import size_position
from wallet import MockWallet, RunCooked

log = logging.getLogger("mock_wallet")


class PaperTrader:
    def __init__(self, wallet: MockWallet):
        self.wallet = wallet

    def _halt_if_cooked(self, result=None):
        if self.wallet.is_cooked:
            raise RunCooked(self.wallet.run_id, self.wallet.cooked_reason or "cooked", result)

    def buy(self, symbol: str, quote_price) -> dict | None:
        """Open a position sized from current equity. None if nothing fits."""
        self._halt_if_cooked()
        size = size_position(self.wallet)
        if size is None:
            log.warning("skip %s: no viable size (balance $%s, equity $%s)",
                        symbol, self.wallet.balance, self.wallet.equity())
            return None
        pos = self.wallet.open_position(symbol, quote_price, size)
        self._halt_if_cooked(pos)
        return pos

    def sell(self, position_id: int, quote_price=None, failed: bool = False,
             reason: str = "exit") -> dict:
        self._halt_if_cooked()
        pos = self.wallet.close_position(position_id, quote_price, failed=failed, reason=reason)
        self._halt_if_cooked(pos)
        return pos

    def mark(self, prices: dict) -> None:
        self._halt_if_cooked()
        self.wallet.mark(prices)
        self._halt_if_cooked()

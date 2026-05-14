"""DataCollector v1 — Phase 1-2 implementation of ``DataCollectorPort``.

Wraps a single ``ExchangePort`` and exposes the market-data surface
Bot Core depends on:

- ``get_current_price`` — passes through to the exchange.
- ``get_market_snapshot`` — combines current price with a fresh
  ``Timestamp``.
- ``get_balances`` — passes through to the exchange.

**No caching.** ``DataCollectorPort`` docstrings say methods are
"potentially cached," but caching introduces freshness/invalidation
questions that depend on the polling rate Bot Core ends up using.
Phase 3's Stage 3.1 ("Data Collector v2") is where we'll add a
caching layer along with the historical-pricing and derived-metrics
work that needs it.

**Error wrapping.** Upstream ``ExchangeError`` instances are
re-raised as ``DataCollectorError`` so callers depend on this
layer's contract rather than the underlying exchange's. The
exception chain (``raise ... from exc``) preserves the original
error for diagnostics.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wobblebot.domain.models import Balance
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.data_collector import DataCollectorPort, MarketSnapshot
from wobblebot.ports.exceptions import DataCollectorError, ExchangeError
from wobblebot.ports.exchange import ExchangePort


class DataCollector(DataCollectorPort):
    """Phase 1-2 DataCollector — thin composer over a single ``ExchangePort``.

    Args:
        exchange: The ``ExchangePort`` instance to query.
    """

    def __init__(self, exchange: ExchangePort) -> None:
        self._exchange = exchange

    async def get_current_price(self, symbol: Symbol) -> Price:
        try:
            return await self._exchange.get_current_price(symbol)
        except ExchangeError as exc:
            raise DataCollectorError(f"Failed to retrieve price for {symbol}: {exc}") from exc

    async def get_market_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        price = await self.get_current_price(symbol)
        return MarketSnapshot(
            symbol=symbol,
            price=price,
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )

    async def get_balances(self) -> list[Balance]:
        try:
            return await self._exchange.get_balances()
        except ExchangeError as exc:
            raise DataCollectorError(f"Failed to retrieve balances: {exc}") from exc

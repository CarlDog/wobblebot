"""DataCollector v2 — Stage 3.1 implementation of ``DataCollectorPort``.

Wires together an ``ExchangePort`` (live ticker, balances) and a
``StoragePort`` (persisted price/trade history) to expose:

- Live read-through: ``get_current_price``, ``get_market_snapshot``,
  ``get_balances`` pass through to the exchange.
- Historical reads: ``get_price_history`` pulls from
  ``StoragePort.get_price_snapshots`` over a configurable
  ``timedelta`` lookback.
- Derived metrics: ``get_volatility``, ``get_max_drawdown``,
  ``get_flatness``, ``get_cycle_stats`` compose a window read with
  the pure-math functions in ``services.metrics``.

**No caching.** Stage 3.2 introduces caching alongside the advisor
loop, where the consumer's polling cadence drives a meaningful TTL.

**Error wrapping.** Upstream ``ExchangeError`` and ``StorageError``
are re-raised as ``DataCollectorError`` so callers depend on this
layer's contract rather than the underlying ports'. The exception
chain (``raise ... from exc``) preserves the original error for
diagnostics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wobblebot.domain.models import Balance, PriceSnapshot
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.data_collector import DataCollectorPort, MarketSnapshot
from wobblebot.ports.exceptions import (
    DataCollectorError,
    ExchangeError,
    StorageError,
)
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort
from wobblebot.services.metrics import (
    CycleStats,
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)

_DEFAULT_LOOKBACK = timedelta(hours=24)


class DataCollector(DataCollectorPort):
    """Stage 3.1 DataCollector v2 — composes an ``ExchangePort`` (live)
    and a ``StoragePort`` (historical) into the consumer-facing
    derived-metrics surface.

    Args:
        exchange: The ``ExchangePort`` instance to query for live data.
        storage: The ``StoragePort`` instance backing historical reads
            (price snapshots persisted by ``cli/observe`` and trades
            persisted by ``cli/live`` / ``cli/shadow``).
    """

    def __init__(self, exchange: ExchangePort, storage: StoragePort) -> None:
        self._exchange = exchange
        self._storage = storage

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

    async def get_price_history(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> list[PriceSnapshot]:
        start_time = datetime.now(UTC) - lookback
        try:
            return await self._storage.get_price_snapshots(symbol=symbol, start_time=start_time)
        except StorageError as exc:
            raise DataCollectorError(f"Failed to load price history for {symbol}: {exc}") from exc

    async def get_volatility(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        snapshots = await self.get_price_history(symbol, lookback)
        return compute_volatility([snap.price.amount for snap in snapshots])

    async def get_max_drawdown(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        snapshots = await self.get_price_history(symbol, lookback)
        return compute_max_drawdown([snap.price.amount for snap in snapshots])

    async def get_flatness(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        snapshots = await self.get_price_history(symbol, lookback)
        return compute_flatness([snap.price.amount for snap in snapshots])

    async def get_cycle_stats(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> CycleStats:
        start_time = datetime.now(UTC) - lookback
        try:
            trades = await self._storage.get_trades(symbol=symbol, start_time=start_time)
        except StorageError as exc:
            raise DataCollectorError(f"Failed to load trade history for {symbol}: {exc}") from exc
        # storage.get_trades returns DESC by executed_at; cycle matching needs ASC.
        trades_ascending = list(reversed(trades))
        return compute_cycle_stats(trades_ascending)

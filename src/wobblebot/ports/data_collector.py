"""DataCollectorPort - Abstract interface for market data aggregation.

This port defines the contract for accessing aggregated market data and metrics.
The Data Collector service implements this port and sits between Bot Core and ExchangePort.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel

from wobblebot.domain.models import Balance, CycleStats, PriceSnapshot
from wobblebot.domain.value_objects import Price, Symbol, Timestamp

_DEFAULT_LOOKBACK = timedelta(hours=24)


class MarketSnapshot(BaseModel):
    """Market data snapshot for a symbol at a point in time.

    Phase 3+ will extend this with volume, volatility, and cycle stats.
    """

    symbol: Symbol
    price: Price
    timestamp: Timestamp

    class Config:
        frozen = True


class DataCollectorPort(ABC):
    """Abstract interface for market data and metrics.

    Phase 1-2: Provides basic market data (prices, balances)
    Phase 3+: Adds historical data, derived metrics (volatility, cycle stats, etc.)

    Implementations:
    - Data Collector service (primary)

    Error convention:
    - Protocol/transport failure raises ``DataCollectorError`` (upstream
      price feed unreachable, derived metric calculation fails, cache
      stampede, etc.). Domain-data miss (e.g. unknown symbol) raises
      rather than returns ``None`` — an unknown symbol is a caller
      bug, not a transient absence.
    """

    @abstractmethod
    async def get_current_price(self, symbol: Symbol) -> Price:
        """Get current market price (potentially cached).

        Args:
            symbol: Trading pair

        Returns:
            Current or recent market price

        Raises:
            DataCollectorError: If price cannot be retrieved
        """

    @abstractmethod
    async def get_market_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        """Get comprehensive market snapshot for a symbol.

        Args:
            symbol: Trading pair

        Returns:
            Market snapshot with price and metadata

        Raises:
            DataCollectorError: If snapshot cannot be retrieved
        """

    @abstractmethod
    async def get_balances(self) -> list[Balance]:
        """Get current account balances (potentially cached).

        Returns:
            List of balances

        Raises:
            DataCollectorError: If balances cannot be retrieved
        """

    # Stage 3.1 — historical reads + derived metrics. Default implementations
    # raise NotImplementedError so an alternative DataCollectorPort impl
    # without a storage backend (e.g. a pure-live mock for unit-testing
    # consumers) can opt in selectively.

    async def get_price_history(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> list[PriceSnapshot]:
        """Return persisted price snapshots within the lookback window.

        Args:
            symbol: Trading pair.
            lookback: How far back from ``now`` to include. Defaults to
                24h.

        Returns:
            Snapshots ordered by ``observed_at`` ASC (oldest first).
            Empty list if no data in the window.

        Raises:
            DataCollectorError: If retrieval fails.
            NotImplementedError: If the implementation has no storage
                backing (e.g. a pure-live test double).
        """
        raise NotImplementedError("Stage 3.1+ feature; requires storage backing")

    async def get_volatility(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        """Sample stdev of simple period-over-period returns.

        See ``services.metrics.compute_volatility`` for the exact
        formula. Returns ``Decimal("0")`` when fewer than three
        snapshots exist in the window (not enough for a defined
        sample stdev) — callers can distinguish "no signal" from
        "computed zero" by inspecting ``get_price_history`` length.

        Args:
            symbol: Trading pair.
            lookback: Window to compute over. Defaults to 24h.

        Raises:
            DataCollectorError: If the underlying read fails.
            NotImplementedError: If the implementation has no storage
                backing.
        """
        raise NotImplementedError("Stage 3.1+ feature; requires storage backing")

    async def get_max_drawdown(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        """Worst peak-to-trough decline over the window (``<= 0``).

        See ``services.metrics.compute_max_drawdown``.

        Raises:
            DataCollectorError: If the underlying read fails.
            NotImplementedError: If the implementation has no storage
                backing.
        """
        raise NotImplementedError("Stage 3.1+ feature; requires storage backing")

    async def get_flatness(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> Decimal:
        """Tightness of price around the mean over the window (``[0, 1]``).

        See ``services.metrics.compute_flatness``.

        Raises:
            DataCollectorError: If the underlying read fails.
            NotImplementedError: If the implementation has no storage
                backing.
        """
        raise NotImplementedError("Stage 3.1+ feature; requires storage backing")

    async def get_cycle_stats(
        self,
        symbol: Symbol,
        lookback: timedelta = _DEFAULT_LOOKBACK,
    ) -> CycleStats:
        """FIFO-matched buy-then-sell cycle stats over the window.

        See ``services.metrics.compute_cycle_stats``. Returns a
        ``CycleStats`` with all zeros if no cycles can be matched.

        Raises:
            DataCollectorError: If the underlying trade-history read
                fails.
            NotImplementedError: If the implementation has no storage
                backing.
        """
        raise NotImplementedError("Stage 3.1+ feature; requires storage backing")

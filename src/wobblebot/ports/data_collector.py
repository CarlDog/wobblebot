"""DataCollectorPort - Abstract interface for market data aggregation.

This port defines the contract for accessing aggregated market data and metrics.
The Data Collector service implements this port and sits between Bot Core and ExchangePort.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from wobblebot.domain.models import Balance
from wobblebot.domain.value_objects import Price, Symbol, Timestamp


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
        pass

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
        pass

    @abstractmethod
    async def get_balances(self) -> list[Balance]:
        """Get current account balances (potentially cached).

        Returns:
            List of balances

        Raises:
            DataCollectorError: If balances cannot be retrieved
        """
        pass

    # Phase 3+ methods (not yet implemented)

    async def get_volatility(self, symbol: Symbol, lookback_hours: int = 24) -> Decimal:
        """Get volatility metric for a symbol.

        Args:
            symbol: Trading pair
            lookback_hours: Historical period for calculation

        Returns:
            Volatility metric (implementation-defined)

        Raises:
            NotImplementedError: Phase 3+ feature
        """
        raise NotImplementedError("Phase 3+ feature")

    async def get_cycle_stats(self, symbol: Symbol) -> dict[str, Any]:
        """Get trading cycle statistics for a symbol.

        Args:
            symbol: Trading pair

        Returns:
            Dict with cycle count, win rate, avg profit, etc.

        Raises:
            NotImplementedError: Phase 3+ feature
        """
        raise NotImplementedError("Phase 3+ feature")

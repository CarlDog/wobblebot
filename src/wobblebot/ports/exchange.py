"""ExchangePort - Abstract interface for exchange interactions.

This port defines the contract for all exchange operations (market data,
orders, balances). Adapters implement this interface for specific exchanges.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import OHLCBar, Price, Symbol


class ExchangePort(ABC):
    """Abstract interface for exchange operations.

    Implementations must handle:
    - Market data retrieval
    - Order placement and management
    - Balance queries
    - Trade history
    - (Phase 4+) Withdrawal operations via exchange API

    Error convention:
    - Domain-data miss returns ``None`` (e.g. unknown order, asset
      never held). Methods that document this in their Returns clause
      use ``T | None``.
    - Protocol / transport / upstream failure raises ``ExchangeError``
      (or a domain exception like ``InsufficientBalance`` when the
      upstream surfaces a business-rule violation).
    """

    @abstractmethod
    async def get_current_price(self, symbol: Symbol) -> Price:
        """Get current market price for a symbol.

        Args:
            symbol: Trading pair

        Returns:
            Current market price

        Raises:
            ExchangeError: If price cannot be retrieved
        """
        pass

    @abstractmethod
    async def get_balances(self) -> list[Balance]:
        """Get all account balances.

        Returns:
            List of balances for all assets

        Raises:
            ExchangeError: If balances cannot be retrieved
        """
        pass

    @abstractmethod
    async def get_balance(self, asset: str) -> Balance | None:
        """Get balance for a specific asset.

        Args:
            asset: Asset code (e.g., BTC, USD)

        Returns:
            Balance for the specified asset, or ``None`` if the account
            has never held it. Kraken's balance endpoint omits
            never-held assets entirely, so adapters distinguish "no
            entry" (return ``None``) from "held it, currently zero"
            (return ``Balance(total=0, ...)``).

        Raises:
            ExchangeError: If balance cannot be retrieved due to a
                transport or protocol failure.
        """
        pass

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """Place a limit order on the exchange.

        Args:
            order: Order to place (status should be 'pending')

        Returns:
            Updated order with exchange_id and status 'open'

        Raises:
            ExchangeError: If order placement fails
            InsufficientBalance: If account lacks sufficient funds
        """
        pass

    @abstractmethod
    async def cancel_order(self, order: Order) -> Order:
        """Cancel an open order.

        Args:
            order: Order to cancel (must have exchange_id)

        Returns:
            Updated order with status 'canceled' (American spelling per
            ADR-005, matching Kraken's canonical vocabulary)

        Raises:
            ExchangeError: If cancellation fails
        """
        pass

    @abstractmethod
    async def get_order_status(self, order: Order) -> Order:
        """Get current status of an order.

        Args:
            order: Order to check (must have exchange_id)

        Returns:
            Updated order with current status and filled_amount

        Raises:
            ExchangeError: If status cannot be retrieved
        """
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        """Get all open orders, optionally filtered by symbol.

        Args:
            symbol: Optional symbol to filter by

        Returns:
            List of open orders

        Raises:
            ExchangeError: If orders cannot be retrieved
        """
        pass

    @abstractmethod
    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        """Get trade history, optionally filtered by symbol.

        Args:
            symbol: Optional symbol to filter by
            limit: Maximum number of trades to return

        Returns:
            List of completed trades

        Raises:
            ExchangeError: If trade history cannot be retrieved
        """
        pass

    @abstractmethod
    async def get_ohlc(
        self,
        symbol: Symbol,
        interval_minutes: int = 1,
        since: datetime | None = None,
    ) -> list[OHLCBar]:
        """Fetch historical OHLC bars for ``symbol``.

        v1.1 addition driving the cli/observe --backfill feature and
        the future backtester/historian. Returns at most one page of
        bars (exchanges typically cap response size — Kraken returns
        up to 720 per call). The caller paginates by passing the last
        returned bar's ``opened_at`` as the next call's ``since``.

        Args:
            symbol: Trading pair.
            interval_minutes: Bar duration. Must be one of
                ``OHLCBar.ALLOWED_INTERVALS`` (Kraken-aligned set).
            since: Exclusive lower bound — only bars STRICTLY AFTER
                this timestamp are returned. ``None`` returns the
                most-recent page available.

        Returns:
            Bars in chronological order. Empty list when no bars match
            the window (newer than ``since``).

        Raises:
            ExchangeError: On transport / protocol / parse failure.
            ValueError: If ``interval_minutes`` is outside the allowed set.
        """

    @abstractmethod
    async def withdraw(self, asset: str, amount: Decimal, destination: str) -> str:
        """Initiate a withdrawal to external account.

        This method is used by the Harvester module (Phase 4+).
        Requires withdrawal-enabled API key.

        Args:
            asset: Asset to withdraw (e.g., USD)
            amount: Amount to withdraw
            destination: Destination identifier (bank account, etc.)

        Returns:
            Withdrawal transaction ID

        Raises:
            ExchangeError: If withdrawal fails
            InsufficientBalance: If insufficient funds
        """
        pass

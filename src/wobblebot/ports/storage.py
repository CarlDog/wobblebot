"""StoragePort - Abstract interface for persistence operations.

This port defines the contract for storing and retrieving domain entities.
Adapters implement this interface for specific storage backends (SQLite, Postgres, etc.).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Symbol


class StoragePort(ABC):
    """Abstract interface for persistence operations.

    Implementations must handle:
    - Order persistence and queries
    - Trade history
    - Balance snapshots
    - Configuration snapshots (future)
    - Audit logs (future)

    Position tracking deferred to Phase 3+ (see ADR-005).

    Error convention:
    - Domain-data miss returns ``None`` (e.g. unknown order id).
      Empty list queries return ``[]``.
    - Protocol failure raises ``StorageError`` (DB unreachable,
      constraint violation that domain layer cannot prevent, etc.).

    Caller contract for concurrent writes:
    - The adapter offers no optimistic concurrency control. Two
      coroutines doing ``get_order(X) -> mutate -> save_order(X)``
      concurrently will clobber each other silently. Callers (Bot
      Core in Phase 2+) MUST serialize per-entity writes themselves —
      e.g. a per-order ``asyncio.Lock`` keyed by ``order.id``.
    """

    # Order operations
    @abstractmethod
    async def save_order(self, order: Order) -> None:
        """Persist an order.

        Args:
            order: Order to save (insert or update)

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_order(self, order_id: UUID) -> Order | None:
        """Retrieve an order by ID.

        Args:
            order_id: Order ID

        Returns:
            Order if found, None otherwise

        Raises:
            StorageError: If retrieval fails
        """
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        """Get all open orders, optionally filtered by symbol.

        Args:
            symbol: Optional symbol filter

        Returns:
            List of open orders

        Raises:
            StorageError: If retrieval fails
        """
        pass

    # Trade operations
    @abstractmethod
    async def save_trade(self, trade: Trade) -> None:
        """Persist a trade.

        Args:
            trade: Trade to save

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_trades(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Get trade history with optional filters.

        Args:
            symbol: Optional symbol filter
            start_time: Optional start time filter
            end_time: Optional end time filter
            limit: Maximum number of trades to return

        Returns:
            List of trades

        Raises:
            StorageError: If retrieval fails
        """
        pass

    # Balance operations
    @abstractmethod
    async def save_balance_snapshot(self, balances: list[Balance]) -> None:
        """Save a snapshot of all balances.

        Args:
            balances: List of balances to snapshot

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_latest_balance_snapshot(self) -> list[Balance]:
        """Get the most recent balance snapshot.

        Returns:
            List of balances from latest snapshot

        Raises:
            StorageError: If retrieval fails
        """
        pass

    # Grid state operations (Stage 2.2)
    @abstractmethod
    async def save_grid_state(self, state: GridState) -> None:
        """Persist or replace the grid anchor for a symbol.

        Per ADR-006 decision 4, only ``GridState`` is persisted —
        ``GridSlot`` is a derived view computed each tick from
        ``compute_grid_levels`` plus a query of open orders.

        Idempotent: saving the same ``state`` twice leaves storage in
        the same shape (one row per symbol, last writer wins).

        Args:
            state: Grid anchor to persist.

        Raises:
            StorageError: If save fails.
        """
        pass

    @abstractmethod
    async def get_grid_state(self, symbol: Symbol) -> GridState | None:
        """Retrieve the grid anchor for a symbol.

        Args:
            symbol: Trading pair.

        Returns:
            ``GridState`` if the engine has previously initialized one
            for this symbol; ``None`` otherwise.

        Raises:
            StorageError: If retrieval fails.
        """
        pass

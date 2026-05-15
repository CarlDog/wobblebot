"""Domain models for WobbleBot.

These models represent core business entities with identity and lifecycle.
They enforce business rules and maintain invariants.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from wobblebot.domain.exceptions import InvalidOrderState
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp


class Order(BaseModel):
    """Represents a limit order on the exchange.

    Aligned with Kraken API field naming (see ADR-005).

    Attributes:
        id: Internal unique identifier (UUID for database)
        exchange_id: Kraken transaction ID (txid) after submission
        symbol: Trading pair
        side: Buy or sell
        price: Limit price
        amount: Order quantity
        status: Order status (Kraken canonical values)
        created_at: When the order was created
        updated_at: When the order was last updated (set on state changes)
        filled_amount: Amount filled so far (for partial fills)

    Status values match Kraken API:
        - pending: Order created locally, not yet submitted
        - open: Order on exchange order book
        - closed: Order fully filled
        - canceled: Order cancelled (American spelling per Kraken)
        - expired: Order expired (time-based)
    """

    id: UUID = Field(default_factory=uuid4, description="Internal order ID")
    exchange_id: str | None = Field(None, description="Kraken txid")
    symbol: Symbol
    side: OrderSide
    price: Price
    amount: Amount
    status: Literal["pending", "open", "closed", "canceled", "expired"] = Field(default="pending")
    created_at: Timestamp
    updated_at: Timestamp | None = Field(None, description="Set on state changes")
    filled_amount: Decimal = Field(default=Decimal("0"), ge=0)

    def mark_open(self, exchange_id: str) -> None:
        """Mark order as open on exchange.

        Args:
            exchange_id: Kraken transaction ID (txid)

        Raises:
            ValueError: If exchange_id is None or empty
        """
        if not exchange_id:
            raise ValueError("exchange_id is required when marking order as open")
        self.exchange_id = exchange_id
        self.status = "open"
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def record_fill(self, filled_amount: Decimal) -> None:
        """Record a fill event from the exchange.

        Accepts the *cumulative* filled amount (matches Kraken's ``vol_exec``).
        Status transitions to ``closed`` only on a full fill; partial fills
        keep ``status='open'`` since the order remains on the book.

        Args:
            filled_amount: Cumulative amount filled so far. Must be in
                [self.filled_amount, self.amount.value].

        Raises:
            ValueError: If filled_amount exceeds order amount, decreases
                from the prior fill amount, or the order is not open.
        """
        if filled_amount > self.amount.value:
            raise ValueError(
                f"Filled amount {filled_amount} cannot exceed order amount {self.amount.value}"
            )
        if filled_amount < self.filled_amount:
            raise ValueError(
                f"Filled amount {filled_amount} cannot decrease from prior "
                f"value {self.filled_amount}; fills are cumulative"
            )
        if self.status != "open":
            raise ValueError(
                f"Cannot record a fill on an order in status '{self.status}'; "
                "only 'open' orders accept fills"
            )
        self.filled_amount = filled_amount
        if filled_amount == self.amount.value:
            self.status = "closed"
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def mark_canceled(self) -> None:
        """Mark order as canceled.

        Raises:
            InvalidOrderState: If order is already closed or expired
        """
        if self.status in ("closed", "expired"):
            raise InvalidOrderState(current_state=self.status, attempted_transition="cancel")
        self.status = "canceled"
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def mark_expired(self) -> None:
        """Mark order as expired (time-based expiration)."""
        self.status = "expired"
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def is_active(self) -> bool:
        """Check if order is active (pending or open)."""
        return self.status in ("pending", "open")

    class Config:
        """Pydantic config."""

        validate_assignment = True  # Validate when attributes are changed


class Trade(BaseModel):
    """Represents a completed trade (executed order).

    Aligned with Kraken API structure (see ADR-005).
    Uses Kraken's txid strings for immutable trade records.

    Attributes:
        id: Kraken trade transaction ID (txid)
        order_id: Kraken order transaction ID that created this trade
        symbol: Trading pair
        side: Buy or sell
        price: Execution price
        amount: Trade volume
        fee: Trading fee (in quote currency)
        cost: Total cost (price × volume)
        executed_at: When the trade was executed
    """

    id: str = Field(..., description="Kraken trade txid")
    order_id: str = Field(..., description="Kraken order txid")
    symbol: Symbol
    side: OrderSide
    price: Price
    amount: Amount
    fee: Decimal = Field(..., description="Fee in quote currency", ge=0)
    cost: Decimal = Field(..., description="Total cost (price × volume)", ge=0)
    executed_at: Timestamp

    class Config:
        """Pydantic config."""

        frozen = True  # Trades are immutable once created


# Note: Position model deferred to Phase 3+ (margin trading)
# For Phase 1-2 spot trading, P&L tracking via Trade records is sufficient
# See ADR-005 for rationale


class Balance(BaseModel):
    """Immutable account balance snapshot for a single asset.

    A balance is a point-in-time reading. To "lock funds for an order"
    you do NOT mutate a Balance — you record the open order in
    ``StoragePort`` and compute ``locked`` at read time from the
    open-order set (or read Kraken's ``hold_trade`` directly via the
    ``ExchangePort``). This keeps the exchange as the source of truth
    and removes the need for in-memory reconciliation.

    Attributes:
        asset: Asset code (e.g., BTC, USD)
        total: Total balance (``available + locked``)
        available: Available balance (not in open orders)
        locked: Balance locked in open orders — mirrors Kraken's
            ``hold_trade``. Informational; computed by the adapter.
        updated_at: When this snapshot was taken.
    """

    asset: str = Field(..., min_length=1, max_length=10)
    total: Decimal = Field(..., ge=0)
    available: Decimal = Field(..., ge=0)
    locked: Decimal = Field(default=Decimal("0"), ge=0)
    updated_at: Timestamp = Field(default_factory=lambda: Timestamp(dt=datetime.now(UTC)))

    class Config:
        """Pydantic config."""

        frozen = True


class PriceSnapshot(BaseModel):
    """Single observed price for a symbol at a point in time.

    Append-only row in the ``price_snapshots`` table. ``cli/observe``
    writes one per poll; ``DataCollector v2`` (Stage 3.1) reads them
    back to compute volatility, drawdown, and other rolling metrics.

    Kept distinct from ``ports.data_collector.MarketSnapshot``: this
    model is the storage row shape and stays narrow, while
    ``MarketSnapshot`` is the DataCollector return shape and is
    expected to grow (volume, indicators, etc.) without disturbing
    the on-disk format.

    Attributes:
        symbol: Trading pair the price is for.
        price: The observed price.
        observed_at: When the observation was made.
    """

    symbol: Symbol
    price: Price
    observed_at: Timestamp

    class Config:
        """Pydantic config."""

        frozen = True


class CycleStats(BaseModel):
    """Realized cycle statistics derived from trade history.

    Lives in the domain layer (not services) so ``DataCollectorPort``
    can name it as a return type without dragging the services package
    into the ports import graph. The math that populates one of these
    is in ``services.metrics.compute_cycle_stats``.

    A "cycle" is a buy-then-sell pair matched FIFO within a symbol.
    Open positions (unmatched buys, or sells with no preceding buy)
    are excluded — they're not yet a completed cycle.

    PnL is computed in quote currency using the executed ``cost``
    fields (price × volume), minus both legs' ``fee``:

        cycle_pnl = sell.cost - buy.cost - buy.fee - sell.fee

    All zeros indicate no completed cycles in the input.

    Attributes:
        cycle_count: Number of matched buy-then-sell pairs.
        win_count: Cycles with positive PnL after fees.
        win_rate: ``win_count / cycle_count``, or ``0`` if no cycles.
        total_pnl: Sum of all cycle PnLs in quote currency.
        avg_profit_per_cycle: ``total_pnl / cycle_count``, or ``0``.
    """

    cycle_count: int
    win_count: int
    win_rate: Decimal
    total_pnl: Decimal
    avg_profit_per_cycle: Decimal

    class Config:
        """Pydantic config."""

        frozen = True

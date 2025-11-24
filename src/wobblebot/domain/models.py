"""Domain models for WobbleBot.

These models represent core business entities with identity and lifecycle.
They enforce business rules and maintain invariants.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

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

    def mark_closed(self, filled_amount: Decimal) -> None:
        """Mark order as closed (fully or partially filled).

        Args:
            filled_amount: Amount filled

        Raises:
            ValueError: If filled amount exceeds order amount
        """
        if filled_amount > self.amount.value:
            raise ValueError(
                f"Filled amount {filled_amount} cannot exceed order amount {self.amount.value}"
            )
        self.filled_amount = filled_amount
        self.status = "closed"
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def mark_canceled(self) -> None:
        """Mark order as canceled.

        Raises:
            InvalidOrderState: If order is already closed or expired
        """
        from wobblebot.domain.exceptions import InvalidOrderState

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
    """Represents an account balance for a single asset.

    Attributes:
        asset: Asset code (e.g., BTC, USD)
        total: Total balance
        available: Available balance (not locked in orders)
        locked: Balance locked in open orders
        updated_at: When balance was last updated
    """

    asset: str = Field(..., min_length=1, max_length=10)
    total: Decimal = Field(..., ge=0)
    available: Decimal = Field(..., ge=0)
    locked: Decimal = Field(default=Decimal("0"), ge=0)
    updated_at: Timestamp = Field(default_factory=lambda: Timestamp(dt=datetime.now(UTC)))

    def lock(self, amount: Decimal) -> None:
        """Lock funds for an order."""
        if amount > self.available:
            from wobblebot.domain.exceptions import InsufficientBalance

            raise InsufficientBalance(
                required=float(amount),
                available=float(self.available),
                asset=self.asset,
            )

        self.available -= amount
        self.locked += amount
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    def unlock(self, amount: Decimal) -> None:
        """Unlock funds (e.g., after order cancellation)."""
        if amount > self.locked:
            raise ValueError(f"Cannot unlock {amount}: exceeds locked amount {self.locked}")
        self.locked -= amount
        self.available += amount
        self.updated_at = Timestamp(dt=datetime.now(UTC))

    class Config:
        """Pydantic config."""

        validate_assignment = True

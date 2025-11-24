"""Tests for domain models (Order, Trade, Balance)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wobblebot.domain.exceptions import InsufficientBalance, InvalidOrderState
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp


class TestOrder:
    """Tests for Order model."""

    def test_order_creation(self):
        """Test creating a new order."""
        symbol = Symbol(base="BTC", quote="USD")
        side = OrderSide(side="buy")
        price = Price(amount=Decimal("50000"), currency="USD")
        amount = Amount(value=Decimal("0.1"), asset="BTC")
        now = Timestamp(dt=datetime.now(timezone.utc))

        order = Order(
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            created_at=now,
        )

        assert order.id is not None  # UUID generated
        assert order.status == "pending"
        assert order.exchange_id is None
        assert order.filled_amount == Decimal("0")
        assert order.updated_at is None  # Not set initially

    def test_mark_open(self):
        """Test marking an order as open."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")

        assert order.status == "open"
        assert order.exchange_id == "kraken-456"

    def test_mark_open_requires_exchange_id(self):
        """Test mark_open requires exchange_id."""
        order = self._create_test_order()
        with pytest.raises(ValueError, match="exchange_id is required"):
            order.mark_open(exchange_id=None)  # type: ignore

    def test_mark_closed_full(self):
        """Test marking an order as fully closed."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.mark_closed(filled_amount=Decimal("0.1"))

        assert order.status == "closed"
        assert order.filled_amount == Decimal("0.1")

    def test_mark_closed_partial(self):
        """Test marking an order as partially closed."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.mark_closed(filled_amount=Decimal("0.05"))

        assert order.status == "closed"
        assert order.filled_amount == Decimal("0.05")

    def test_mark_closed_validates_amount(self):
        """Test mark_closed validates filled amount."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")

        with pytest.raises(ValueError, match="cannot exceed order amount"):
            order.mark_closed(filled_amount=Decimal("1.0"))  # More than order amount

    def test_mark_canceled(self):
        """Test marking an order as canceled."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.mark_canceled()

        assert order.status == "canceled"

    def test_mark_expired(self):
        """Test marking an order as expired."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.mark_expired()

        assert order.status == "expired"

    def test_is_active(self):
        """Test is_active method."""
        order = self._create_test_order()

        assert order.is_active()  # pending

        order.mark_open(exchange_id="kraken-456")
        assert order.is_active()  # open

        order.mark_closed(filled_amount=Decimal("0.1"))
        assert not order.is_active()  # closed

    def test_status_transition_validation(self):
        """Test invalid status transitions are caught."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.mark_closed(filled_amount=Decimal("0.1"))  # Closed

        # Cannot cancel a closed order
        with pytest.raises(InvalidOrderState):
            order.mark_canceled()

    def _create_test_order(self) -> Order:
        """Helper to create a test order."""
        now = Timestamp(dt=datetime.now(timezone.utc))
        return Order(
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide(side="buy"),
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            created_at=now,
        )


class TestTrade:
    """Tests for Trade model."""

    def test_trade_creation(self):
        """Test creating a trade."""
        symbol = Symbol(base="BTC", quote="USD")
        side = OrderSide(side="sell")
        price = Price(amount=Decimal("51000"), currency="USD")
        amount = Amount(value=Decimal("0.1"), asset="BTC")
        executed_at = Timestamp(dt=datetime.now(timezone.utc))

        trade = Trade(
            id="TXID-789",  # String ID (Kraken txid)
            order_id="ORDERID-123",  # String order ID
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            fee=Decimal("5.10"),  # Decimal, not Amount
            cost=Decimal("5100.00"),  # price × volume
            executed_at=executed_at,
        )

        assert trade.id == "TXID-789"
        assert trade.order_id == "ORDERID-123"
        assert trade.fee == Decimal("5.10")
        assert trade.cost == Decimal("5100.00")

    def test_trade_immutability(self):
        """Test that Trade is frozen."""
        trade = self._create_test_trade()

        with pytest.raises(Exception):  # Pydantic raises ValidationError
            trade.fee = Decimal("10.00")  # type: ignore

    def _create_test_trade(self) -> Trade:
        """Helper to create a test trade."""
        return Trade(
            id="TEST-TXID",  # String ID
            order_id="TEST-ORDER",  # String order ID
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide(side="sell"),
            price=Price(amount=Decimal("51000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            fee=Decimal("5.10"),
            cost=Decimal("5100.00"),
            executed_at=Timestamp(dt=datetime.now(timezone.utc)),
        )


# Note: Position model tests removed - Position deferred to Phase 3+ (see ADR-005)


class TestBalance:
    """Tests for Balance model."""

    def test_balance_creation(self):
        """Test creating a balance."""
        balance = Balance(
            asset="BTC",
            total=Decimal("1.5"),
            available=Decimal("1.5"),
        )

        assert balance.asset == "BTC"
        assert balance.total == Decimal("1.5")
        assert balance.available == Decimal("1.5")
        assert balance.locked == Decimal("0")

    def test_lock_funds(self):
        """Test locking funds."""
        balance = Balance(asset="BTC", total=Decimal("1.5"), available=Decimal("1.5"))

        balance.lock(amount=Decimal("0.5"))

        assert balance.available == Decimal("1.0")
        assert balance.locked == Decimal("0.5")
        assert balance.total == Decimal("1.5")  # Total unchanged

    def test_lock_insufficient_funds(self):
        """Test locking more than available raises exception."""
        balance = Balance(asset="BTC", total=Decimal("1.0"), available=Decimal("1.0"))

        with pytest.raises(InsufficientBalance):
            balance.lock(amount=Decimal("1.5"))

    def test_unlock_funds(self):
        """Test unlocking funds."""
        balance = Balance(asset="BTC", total=Decimal("1.5"), available=Decimal("1.0"))
        balance.lock(amount=Decimal("0.5"))  # Lock 0.5, available now 0.5
        assert balance.locked == Decimal("0.5")
        assert balance.available == Decimal("0.5")

        balance.unlock(amount=Decimal("0.3"))  # Unlock 0.3

        assert balance.available == Decimal("0.8")  # 0.5 + 0.3
        assert balance.locked == Decimal("0.2")  # 0.5 - 0.3

    def test_unlock_validates_amount(self):
        """Test unlock validates amount."""
        balance = Balance(asset="BTC", total=Decimal("1.5"), available=Decimal("1.0"))
        balance.lock(amount=Decimal("0.5"))

        with pytest.raises(ValueError, match="exceeds locked amount"):
            balance.unlock(amount=Decimal("1.0"))  # More than locked

    def test_lock_unlock_roundtrip(self):
        """Test lock/unlock roundtrip."""
        balance = Balance(asset="USD", total=Decimal("1000"), available=Decimal("1000"))

        balance.lock(amount=Decimal("100"))
        assert balance.available == Decimal("900")
        assert balance.locked == Decimal("100")

        balance.unlock(amount=Decimal("100"))
        assert balance.available == Decimal("1000")
        assert balance.locked == Decimal("0")

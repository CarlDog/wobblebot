"""Tests for domain models (Order, Trade, Balance)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wobblebot.domain.exceptions import InvalidOrderState
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp


class TestOrder:
    """Tests for Order model."""

    def test_order_creation(self):
        """Test creating a new order."""
        symbol = Symbol(base="BTC", quote="USD")
        side = OrderSide.BUY
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

    def test_record_fill_full_closes_order(self):
        """A fill equal to the order amount transitions status to closed."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.record_fill(filled_amount=Decimal("0.1"))

        assert order.status == "closed"
        assert order.filled_amount == Decimal("0.1")

    def test_record_fill_partial_keeps_open(self):
        """A partial fill leaves status as open - the order remains on the book."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.record_fill(filled_amount=Decimal("0.03"))

        assert order.status == "open"
        assert order.filled_amount == Decimal("0.03")

    def test_record_fill_accumulates_then_closes(self):
        """Successive cumulative fills accumulate; full fill closes the order."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.record_fill(filled_amount=Decimal("0.04"))
        assert order.status == "open"
        order.record_fill(filled_amount=Decimal("0.07"))
        assert order.status == "open"
        assert order.filled_amount == Decimal("0.07")
        order.record_fill(filled_amount=Decimal("0.1"))
        assert order.status == "closed"

    def test_record_fill_rejects_decrease(self):
        """Fills are cumulative; a decrease must raise."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.record_fill(filled_amount=Decimal("0.05"))

        with pytest.raises(ValueError, match="cannot decrease"):
            order.record_fill(filled_amount=Decimal("0.03"))

    def test_record_fill_rejects_overfill(self):
        """A fill larger than the order amount must raise."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")

        with pytest.raises(ValueError, match="cannot exceed order amount"):
            order.record_fill(filled_amount=Decimal("1.0"))

    def test_record_fill_requires_open_status(self):
        """record_fill rejects orders that aren't currently open."""
        order = self._create_test_order()  # status='pending'
        with pytest.raises(ValueError, match="only 'open' orders"):
            order.record_fill(filled_amount=Decimal("0.05"))

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

        order.record_fill(filled_amount=Decimal("0.1"))
        assert not order.is_active()  # closed

    def test_status_transition_validation(self):
        """Test invalid status transitions are caught."""
        order = self._create_test_order()
        order.mark_open(exchange_id="kraken-456")
        order.record_fill(filled_amount=Decimal("0.1"))  # Closed

        # Cannot cancel a closed order
        with pytest.raises(InvalidOrderState):
            order.mark_canceled()

    def _create_test_order(self) -> Order:
        """Helper to create a test order."""
        now = Timestamp(dt=datetime.now(timezone.utc))
        return Order(
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide.BUY,
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            created_at=now,
        )


class TestTrade:
    """Tests for Trade model."""

    def test_trade_creation(self):
        """Test creating a trade."""
        symbol = Symbol(base="BTC", quote="USD")
        side = OrderSide.SELL
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
            side=OrderSide.SELL,
            price=Price(amount=Decimal("51000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            fee=Decimal("5.10"),
            cost=Decimal("5100.00"),
            executed_at=Timestamp(dt=datetime.now(timezone.utc)),
        )


# Note: Position model tests removed - Position deferred to Phase 3+ (see ADR-005)


class TestBalance:
    """Tests for the Balance immutable snapshot."""

    def test_balance_creation(self):
        """Constructing a balance with explicit fields."""
        balance = Balance(
            asset="BTC",
            total=Decimal("1.5"),
            available=Decimal("1.5"),
        )

        assert balance.asset == "BTC"
        assert balance.total == Decimal("1.5")
        assert balance.available == Decimal("1.5")
        assert balance.locked == Decimal("0")

    def test_balance_is_frozen(self):
        """A Balance is an immutable snapshot - attribute assignment must raise."""
        balance = Balance(asset="BTC", total=Decimal("1.5"), available=Decimal("1.5"))
        with pytest.raises(Exception):  # Pydantic ValidationError on frozen model
            balance.available = Decimal("1.0")  # type: ignore

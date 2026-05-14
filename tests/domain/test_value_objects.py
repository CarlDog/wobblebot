"""Tests for domain value objects."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp


class TestSymbol:
    """Tests for Symbol value object."""

    def test_symbol_creation(self):
        """Test creating a valid symbol."""
        symbol = Symbol(base="BTC", quote="USD")
        assert symbol.base == "BTC"
        assert symbol.quote == "USD"

    def test_symbol_uppercase_conversion(self):
        """Test automatic uppercase conversion."""
        symbol = Symbol(base="btc", quote="usd")
        assert symbol.base == "BTC"
        assert symbol.quote == "USD"

    def test_symbol_string_format(self):
        """Test string representation."""
        symbol = Symbol(base="ETH", quote="USD")
        assert str(symbol) == "ETH/USD"

    def test_symbol_immutability(self):
        """Test that Symbol is immutable."""
        symbol = Symbol(base="BTC", quote="USD")
        with pytest.raises(ValidationError):
            symbol.base = "ETH"  # type: ignore

    def test_symbol_validation_empty(self):
        """Test validation rejects empty strings."""
        with pytest.raises(ValidationError):
            Symbol(base="", quote="USD")


class TestPrice:
    """Tests for Price value object."""

    def test_price_creation(self):
        """Test creating a valid price."""
        price = Price(amount=Decimal("50000.50"), currency="USD")
        assert price.amount == Decimal("50000.50")
        assert price.currency == "USD"

    def test_price_positive_validation(self):
        """Test price must be positive."""
        with pytest.raises(ValidationError):
            Price(amount=Decimal("0"), currency="USD")

        with pytest.raises(ValidationError):
            Price(amount=Decimal("-100"), currency="USD")

    def test_price_uppercase_currency(self):
        """Test automatic uppercase conversion."""
        price = Price(amount=Decimal("100"), currency="usd")
        assert price.currency == "USD"

    def test_price_string_format(self):
        """Test string representation."""
        price = Price(amount=Decimal("50000.123456"), currency="USD")
        assert "50000.12345600" in str(price)
        assert "USD" in str(price)

    def test_price_immutability(self):
        """Test that Price is immutable."""
        price = Price(amount=Decimal("100"), currency="USD")
        with pytest.raises(ValidationError):
            price.amount = Decimal("200")  # type: ignore


class TestAmount:
    """Tests for Amount value object."""

    def test_amount_creation(self):
        """Test creating a valid amount."""
        amount = Amount(value=Decimal("1.5"), asset="BTC")
        assert amount.value == Decimal("1.5")
        assert amount.asset == "BTC"

    def test_amount_positive_validation(self):
        """Test amount must be positive."""
        with pytest.raises(ValidationError):
            Amount(value=Decimal("0"), asset="BTC")

        with pytest.raises(ValidationError):
            Amount(value=Decimal("-0.5"), asset="BTC")

    def test_amount_uppercase_asset(self):
        """Test automatic uppercase conversion."""
        amount = Amount(value=Decimal("1.5"), asset="btc")
        assert amount.asset == "BTC"

    def test_amount_string_format(self):
        """Test string representation."""
        amount = Amount(value=Decimal("1.23456789"), asset="BTC")
        assert "1.23456789" in str(amount)
        assert "BTC" in str(amount)

    def test_amount_immutability(self):
        """Test that Amount is immutable."""
        amount = Amount(value=Decimal("1.5"), asset="BTC")
        with pytest.raises(ValidationError):
            amount.value = Decimal("2.0")  # type: ignore


class TestOrderSide:
    """Tests for OrderSide enum."""

    def test_buy_side(self):
        """OrderSide.BUY is the canonical buy value."""
        assert OrderSide.BUY == "buy"
        assert OrderSide("buy") is OrderSide.BUY
        assert str(OrderSide.BUY) == "buy"

    def test_sell_side(self):
        """OrderSide.SELL is the canonical sell value."""
        assert OrderSide.SELL == "sell"
        assert OrderSide("sell") is OrderSide.SELL
        assert str(OrderSide.SELL) == "sell"

    def test_invalid_side(self):
        """Constructing from an unknown string raises ValueError."""
        with pytest.raises(ValueError):
            OrderSide("long")


class TestTimestamp:
    """Tests for Timestamp value object."""

    def test_timestamp_creation(self):
        """Test creating a valid timestamp."""
        dt = datetime.now(timezone.utc)
        ts = Timestamp(dt=dt)
        assert ts.dt == dt

    def test_timestamp_requires_timezone(self):
        """Test that naive datetime is rejected."""
        naive_dt = datetime.now()  # No timezone
        with pytest.raises(ValidationError):
            Timestamp(dt=naive_dt)

    def test_timestamp_string_format(self):
        """Test ISO 8601 string format."""
        dt = datetime(2025, 11, 24, 12, 30, 45, tzinfo=timezone.utc)
        ts = Timestamp(dt=dt)
        assert "2025-11-24T12:30:45" in str(ts)

    def test_timestamp_to_unix_ms(self):
        """Test conversion to Unix timestamp in milliseconds."""
        dt = datetime(2025, 11, 24, 12, 30, 45, tzinfo=timezone.utc)
        ts = Timestamp(dt=dt)
        unix_ms = ts.to_unix_ms()
        assert isinstance(unix_ms, int)
        assert unix_ms > 0

    def test_timestamp_to_unix_seconds(self):
        """Test conversion to Unix timestamp in seconds (Kraken format)."""
        dt = datetime(2025, 11, 24, 12, 30, 45, 123456, tzinfo=timezone.utc)
        ts = Timestamp(dt=dt)
        unix_sec = ts.to_unix_seconds()
        assert isinstance(unix_sec, float)
        assert unix_sec > 0
        # Verify microseconds preserved as decimal
        assert str(unix_sec).endswith("123456")

    def test_timestamp_immutability(self):
        """Test that Timestamp is immutable."""
        dt = datetime.now(timezone.utc)
        ts = Timestamp(dt=dt)
        with pytest.raises(ValidationError):
            ts.dt = datetime.now(timezone.utc)  # type: ignore

    def test_timestamp_normalizes_to_utc(self):
        """Non-UTC tz-aware inputs are converted to UTC.

        This is what guarantees that .isoformat() always ends in +00:00
        and that ISO 8601 TEXT ordering in SQLite matches chronological
        ordering. Without it, '2026-01-01T07:00:00-05:00' and
        '2026-01-01T13:00:00+00:00' (same instant) would sort wrong.
        """
        from datetime import timedelta

        eastern = timezone(timedelta(hours=-5))
        ts = Timestamp(dt=datetime(2026, 1, 1, 12, 0, 0, tzinfo=eastern))
        assert ts.dt.tzinfo == timezone.utc
        assert ts.dt.hour == 17  # 12:00 EST -> 17:00 UTC
        assert ts.dt.isoformat().endswith("+00:00")

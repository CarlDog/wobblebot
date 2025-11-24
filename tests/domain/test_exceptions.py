"""Tests for domain exceptions."""

from decimal import Decimal

from wobblebot.domain.exceptions import (
    DailySpendCapExceeded,
    ExposureLimitExceeded,
    InsufficientBalance,
    InvalidAmount,
    InvalidGridConfiguration,
    InvalidOrderState,
    InvalidPriceRange,
    WobbleBotDomainError,
)


class TestExceptionHierarchy:
    """Tests for exception inheritance."""

    def test_all_exceptions_inherit_from_base(self):
        """Test all custom exceptions inherit from WobbleBotDomainError."""
        exceptions = [
            ExposureLimitExceeded(current=100, limit=50),
            DailySpendCapExceeded(spent_today=200, cap=100),
            InvalidOrderState(current_state="filled", attempted_transition="cancel"),
            InvalidGridConfiguration("Invalid spacing"),
            InsufficientBalance(required=100, available=50, asset="BTC"),
            InvalidPriceRange("Min > Max"),
            InvalidAmount("Cannot be negative"),
        ]

        for exc in exceptions:
            assert isinstance(exc, WobbleBotDomainError)


class TestExposureLimitExceeded:
    """Tests for ExposureLimitExceeded exception."""

    def test_exception_message_with_decimals(self):
        """Test exception message formatting with Decimal values."""
        exc = ExposureLimitExceeded(current=Decimal("150.50"), limit=Decimal("100.00"))
        assert "150.50" in str(exc)
        assert "100.00" in str(exc)
        assert "exposure limit exceeded" in str(exc).lower()

    def test_exception_message_with_floats(self):
        """Test exception message formatting with float values."""
        exc = ExposureLimitExceeded(current=150.5, limit=100.0)
        assert "150.5" in str(exc)
        assert "100.0" in str(exc)


class TestDailySpendCapExceeded:
    """Tests for DailySpendCapExceeded exception."""

    def test_exception_message(self):
        """Test exception message formatting."""
        exc = DailySpendCapExceeded(spent_today=Decimal("250.00"), cap=Decimal("200.00"))
        assert "250.00" in str(exc)
        assert "200.00" in str(exc)
        assert "daily spend cap exceeded" in str(exc).lower()


class TestInvalidOrderState:
    """Tests for InvalidOrderState exception."""

    def test_exception_message(self):
        """Test exception message formatting."""
        exc = InvalidOrderState(current_state="filled", attempted_transition="cancel")
        assert "filled" in str(exc)
        assert "cancel" in str(exc)
        assert "invalid transition" in str(exc).lower()


class TestInvalidGridConfiguration:
    """Tests for InvalidGridConfiguration exception."""

    def test_exception_with_message(self):
        """Test exception with custom message."""
        exc = InvalidGridConfiguration("Grid spacing too small")
        assert "Grid spacing too small" in str(exc)


class TestInsufficientBalance:
    """Tests for InsufficientBalance exception."""

    def test_exception_message(self):
        """Test exception message formatting."""
        exc = InsufficientBalance(
            required=Decimal("1.5"), available=Decimal("0.8"), asset="BTC"
        )
        assert "1.5" in str(exc)
        assert "0.8" in str(exc)
        assert "BTC" in str(exc)
        assert "insufficient" in str(exc).lower()


class TestInvalidPriceRange:
    """Tests for InvalidPriceRange exception."""

    def test_exception_message(self):
        """Test exception message formatting."""
        exc = InvalidPriceRange("Min price 1000 > Max price 500")
        assert "1000" in str(exc)
        assert "500" in str(exc)


class TestInvalidAmount:
    """Tests for InvalidAmount exception."""

    def test_exception_with_reason(self):
        """Test exception message with reason."""
        exc = InvalidAmount("Amount -10 cannot be negative")
        assert "-10" in str(exc)
        assert "negative" in str(exc)

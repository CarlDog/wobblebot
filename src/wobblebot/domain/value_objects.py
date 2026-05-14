"""Domain value objects for WobbleBot.

Value objects are immutable, validated data containers that represent
concepts without identity. They enforce domain invariants at creation time.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Symbol(BaseModel):
    """Trading pair symbol (e.g., BTC/USD, ETH/USD).

    Attributes:
        base: Base currency (e.g., BTC, ETH)
        quote: Quote currency (e.g., USD, USDT)
    """

    base: str = Field(..., min_length=1, max_length=10, description="Base currency")
    quote: str = Field(..., min_length=1, max_length=10, description="Quote currency")

    @field_validator("base", "quote")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        """Ensure currency codes are uppercase."""
        return v.upper().strip()

    def __str__(self) -> str:
        """Format as BASE/QUOTE."""
        return f"{self.base}/{self.quote}"

    class Config:
        """Pydantic config."""

        frozen = True  # Make immutable


class Price(BaseModel):
    """Price value with validation.

    Attributes:
        amount: Decimal amount (must be positive)
        currency: Currency code (e.g., USD)
    """

    amount: Decimal = Field(..., gt=0, description="Price amount (must be positive)")
    currency: str = Field(..., min_length=1, max_length=10, description="Currency code")

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        """Ensure currency code is uppercase."""
        return v.upper().strip()

    def __str__(self) -> str:
        """Format as amount + currency."""
        return f"{self.amount:.8f} {self.currency}"

    class Config:
        """Pydantic config."""

        frozen = True  # Make immutable


class Amount(BaseModel):
    """Quantity/amount value with validation.

    Attributes:
        value: Decimal value (must be positive)
        asset: Asset code (e.g., BTC, USD)
    """

    value: Decimal = Field(..., gt=0, description="Amount value (must be positive)")
    asset: str = Field(..., min_length=1, max_length=10, description="Asset code")

    @field_validator("asset")
    @classmethod
    def uppercase_asset(cls, v: str) -> str:
        """Ensure asset code is uppercase."""
        return v.upper().strip()

    def __str__(self) -> str:
        """Format as value + asset."""
        return f"{self.value:.8f} {self.asset}"

    class Config:
        """Pydantic config."""

        frozen = True  # Make immutable


class OrderSide(StrEnum):
    """Order side. StrEnum so the value compares as a plain string for
    SQL drivers and JSON, while still giving us symbolic constants in
    code.
    """

    BUY = "buy"
    SELL = "sell"


class Timestamp(BaseModel):
    """Timestamp value object.

    Attributes:
        dt: UTC datetime
    """

    dt: datetime = Field(..., description="UTC datetime")

    @field_validator("dt")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        """Require timezone-aware input and normalize the value to UTC.

        Storing every timestamp in UTC keeps ISO 8601 string ordering
        consistent with chronological ordering — important for the
        SQLite adapter, which ORDER BYs on the TEXT representation.
        """
        if v.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")
        return v.astimezone(UTC)

    def __str__(self) -> str:
        """Format as ISO 8601."""
        return self.dt.isoformat()

    def to_unix_ms(self) -> int:
        """Convert to Unix timestamp in milliseconds."""
        return int(self.dt.timestamp() * 1000)

    def to_unix_seconds(self) -> float:
        """Convert to Unix timestamp in seconds (epoch).

        Common wire format for exchange APIs that timestamp with
        second granularity. Use ``to_unix_ms`` when sub-second
        precision matters.
        """
        return self.dt.timestamp()

    class Config:
        """Pydantic config."""

        frozen = True  # Make immutable

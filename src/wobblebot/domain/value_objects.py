"""Domain value objects for WobbleBot.

Value objects are immutable, validated data containers that represent
concepts without identity. They enforce domain invariants at creation time.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

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

    @classmethod
    def from_string(cls, raw: str) -> "Symbol":
        """Parse ``"BASE/QUOTE"`` form into a ``Symbol``.

        Single canonical entry point for symbol-string parsing — every
        CLI and config layer converges on this method instead of
        duplicating the split. Raises ``ValueError`` on malformed
        input (missing slash, empty side).
        """
        parts = raw.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Symbol must be BASE/QUOTE form (e.g. BTC/USD); got {raw!r}")
        return cls(base=parts[0], quote=parts[1])

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


class OHLCBar(BaseModel):
    """One OHLC (Open / High / Low / Close) candlestick bar.

    Canonical historical market-data unit. Kraken's ``/0/public/OHLC``
    endpoint returns these; the v1.1 backfill feature persists them
    via cli/observe so the advisor + backtester can reason about
    historical price action without depending on cli/observe having
    been live during the window of interest.

    Per Kraken's documented bar intervals; any other value raises at
    construction so callers get an immediate fail-fast rather than a
    silent "your bars don't fit our schema" mismatch later.

    Attributes:
        symbol: Trading pair the bar describes.
        interval_minutes: Bar duration. One of Kraken's published
            intervals — see ``ALLOWED_INTERVALS``.
        opened_at: Start of the bar's interval (UTC).
        open: Price at the start of the interval.
        high: Highest price during the interval.
        low: Lowest price during the interval.
        close: Price at the end of the interval.
        vwap: Volume-weighted average price during the interval.
        volume: Total volume traded in the interval.
        count: Number of trades in the interval.
    """

    ALLOWED_INTERVALS: ClassVar[frozenset[int]] = frozenset(
        # Kraken's published OHLC intervals (minutes):
        # 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 15d.
        # Source: https://docs.kraken.com/rest/#tag/Market-Data/operation/getOHLCData
        {1, 5, 15, 30, 60, 240, 1440, 10080, 21600}
    )

    symbol: Symbol
    interval_minutes: int = Field(..., description="Bar duration in minutes")
    opened_at: datetime = Field(..., description="UTC start of the interval")
    open: Decimal = Field(..., ge=0)
    high: Decimal = Field(..., ge=0)
    low: Decimal = Field(..., ge=0)
    close: Decimal = Field(..., ge=0)
    vwap: Decimal = Field(..., ge=0)
    volume: Decimal = Field(..., ge=0)
    count: int = Field(..., ge=0)

    @field_validator("interval_minutes")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        if v not in cls.ALLOWED_INTERVALS:
            raise ValueError(
                f"interval_minutes must be one of {sorted(cls.ALLOWED_INTERVALS)}; got {v}"
            )
        return v

    @field_validator("opened_at")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        """Require tz-aware input and normalize to UTC — same contract
        as ``Timestamp``. ISO-8601 string ordering then matches
        chronological ordering, which the SQLite storage layer relies
        on."""
        if v.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        return v.astimezone(UTC)

    class Config:
        """Pydantic config."""

        frozen = True


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

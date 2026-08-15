"""Domain value objects for WobbleBot.

Value objects are immutable, validated data containers that represent
concepts without identity. They enforce domain invariants at creation time.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


def fmt_decimal(value: Decimal, *, max_significant: int | None = None) -> str:
    """Render a ``Decimal`` the way an operator reads it, not as stored.

    Lives in ``domain`` because BOTH ``services`` and ``cli`` need it and
    domain is the only layer both may import (it started in
    ``cli/_common`` and had to move the moment ``services/cost_basis``
    wanted it — see the P3 logging installments).

    Two failure modes it fixes, both observed in production logs:

    - **Storage scale.** Storage and Kraken return full-scale values, so
      ``%s`` prints ``342.18000000`` for a $342.18 withdrawal.
    - **Division tails.** A ``Decimal / Decimal`` keeps 28 significant
      digits, so an average-cost line rendered
      ``73390.78543435964243143764881`` and a loss percentage
      ``8.742335416342072886749045064``. Unreadable in a WARNING an
      operator is meant to scan.

    Also normalizes exponent form: a round ``Decimal("100")`` prints
    ``1E+2`` under ``%s``, and E-notation in a money line is a genuine
    misread risk.

    Trailing zeros are stripped WITHOUT forcing a fixed scale, so this
    stays honest for asset amounts — quantizing to 2dp would render a
    live BTC quantity as ``0.00``. Callers that want a fixed precision
    (a loss percentage, say) should quantize FIRST and pass the result.

    ``max_significant`` caps SIGNIFICANT DIGITS, which is the only thing
    that helps a division result: ``73390.78543435964243143764881`` has
    no trailing zeros to strip, so stripping alone leaves it unreadable.
    Capping by significant digits rather than decimal places keeps the
    same setting usable across magnitudes — 10 digits gives
    ``73390.78543`` for a BTC price and ``0.0694757`` for a DOGE one,
    where a fixed 2dp would destroy the latter.

    Display only. Never feed the output back into arithmetic,
    persistence, or an exchange call.
    """
    if max_significant is not None and value != 0:
        # adjusted() is the exponent of the leading digit, so this
        # exponent keeps exactly `max_significant` digits.
        exponent = value.adjusted() - max_significant + 1
        if exponent < 0:
            value = value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_UP)
    normalized = value.normalize()
    # normalize() produces exponent form for integral values (100 ->
    # 1E+2); re-quantizing to exponent 0 restores plain digits.
    if normalized.as_tuple().exponent > 0:  # type: ignore[operator]
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def fmt_usd(value: Decimal | float | int, *, signed: bool = False) -> str:
    """Render a USD amount the one house way, across every surface.

    Before 2026-08-15 each surface had its own dialect: web templates
    used fixed ``%.2f`` with no thousands separators, Discord embeds used
    ``:,.2f`` / ``:,.4f`` WITH separators, and the same field could
    differ between two embeds (session PnL was 2dp in the loss-cap
    message and 4dp in the session-end one). Worst, fixed 2dp destroyed
    sub-dollar prices: DOGE at $0.0698 rendered "$0.07", and its grid
    levels 3% apart all collapsed to "$0.09" — the ladder was illegible
    on the dashboard. One formatter, used by web filters, Discord
    renderers, and any operator-facing string, ends the drift:

    - ``>= $1``: two decimals with thousands separators — ``$63,237.60``.
    - ``< $1``: four significant digits, because cents are the wrong
      resolution there — ``$0.0698``, ``$0.001296``. Fee-scale values
      keep their meaning: ``$0.0169``, not ``$0.02``.
    - ``signed=True`` (PnL): an explicit ``+``/``-`` before the ``$`` —
      ``+$0.13``, ``-$5.00``. Unsigned negatives still carry the minus.
    - Exact zero renders ``$0.00`` (or unsigned even when ``signed`` —
      a zero PnL is neither a gain nor a loss).

    Display only — never feed the output back into arithmetic. Floats
    are accepted because template/route code hands them over; they pass
    through ``Decimal(str(...))`` so binary tails don't leak into the
    rendering.
    """
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    sign = "-" if dec < 0 else ("+" if signed and dec > 0 else "")
    magnitude = abs(dec)
    if magnitude >= 1 or magnitude == 0:
        body = f"{magnitude.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    else:
        # Four significant digits, then strip trailing zeros the same
        # way fmt_decimal does — "$0.1000" would read as false precision.
        body = fmt_decimal(magnitude, max_significant=4)
    return f"{sign}${body}"


def fmt_qty(value: Decimal | float | int) -> str:
    """Render an asset quantity: at most 8 decimals, no trailing zeros.

    The 8-decimal cap matches Kraken's native asset precision (and the
    fixed ``:.8f`` every surface already used); stripping trailing
    zeros is the fmt_decimal readability rule applied to quantities —
    ``0.01`` beats ``0.01000000`` in a table an operator scans. Display
    only, like its siblings.
    """
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    quantized = dec.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return fmt_decimal(quantized)


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


class Ticker(BaseModel):
    """Top-of-book market snapshot (ADR-025 pre-placement spread guard).

    ``spread_percentage`` is derived from ``bid``/``ask`` at
    construction (mid-price basis) rather than stored input, so it can
    never drift from the two prices it describes.
    """

    symbol: Symbol
    last: Decimal = Field(..., gt=0, description="Last-trade price")
    bid: Decimal = Field(..., gt=0, description="Best bid (highest buy order)")
    ask: Decimal = Field(..., gt=0, description="Best ask (lowest sell order)")

    @model_validator(mode="after")
    def _validate_bid_below_ask(self) -> "Ticker":
        if self.bid >= self.ask:
            raise ValueError(f"bid ({self.bid}) must be less than ask ({self.ask})")
        return self

    @property
    def spread_percentage(self) -> Decimal:
        """Bid-ask spread as a percentage of the mid-price."""
        mid = (self.bid + self.ask) / Decimal("2")
        return (self.ask - self.bid) / mid * Decimal("100")

    class Config:
        """Pydantic config."""

        frozen = True


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

    @model_validator(mode="after")
    def _validate_ohlc_ordering(self) -> "OHLCBar":
        """Enforce ``low <= open/close <= high`` (P2 spine gate).

        A garbled wire response or a malformed CSV row from the
        historical import would otherwise persist permanently via the
        idempotent ``INSERT OR IGNORE`` write path, and every high/low-
        keyed indicator (ATR / Bollinger / Stochastic) would propagate
        the corruption into advisor + auditor outputs. ``vwap`` is
        deliberately unconstrained — Kraken sends 0 for empty bars and
        the import synthesizes 0 (no vwap column in the dump).
        """
        if self.low > self.high:
            raise ValueError(f"low {self.low} exceeds high {self.high}")
        if not self.low <= self.open <= self.high:
            raise ValueError(f"open {self.open} outside [low {self.low}, high {self.high}]")
        if not self.low <= self.close <= self.high:
            raise ValueError(f"close {self.close} outside [low {self.low}, high {self.high}]")
        return self

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

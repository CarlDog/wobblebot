"""Tests for the OHLCBar value object.

Pins the v1.1 backfill substrate's core domain type: every adapter,
service, and storage layer that handles historical bars passes
through OHLCBar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import OHLCBar, Symbol

pytestmark = pytest.mark.unit


_BTC_USD = Symbol(base="BTC", quote="USD")
_NOW_UTC = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _make(**overrides: object) -> OHLCBar:
    """Construct a valid OHLCBar with sensible defaults; override as needed."""
    base: dict[str, object] = {
        "symbol": _BTC_USD,
        "interval_minutes": 1,
        "opened_at": _NOW_UTC,
        "open": Decimal("79000"),
        "high": Decimal("79100"),
        "low": Decimal("78950"),
        "close": Decimal("79050"),
        "vwap": Decimal("79025"),
        "volume": Decimal("1.5"),
        "count": 42,
    }
    base.update(overrides)
    return OHLCBar(**base)  # type: ignore[arg-type]


class TestOHLCBarHappyPath:
    def test_constructs_with_valid_fields(self) -> None:
        bar = _make()
        assert bar.symbol == _BTC_USD
        assert bar.interval_minutes == 1
        assert bar.opened_at == _NOW_UTC
        assert bar.open == Decimal("79000")
        assert bar.count == 42

    def test_is_frozen(self) -> None:
        bar = _make()
        with pytest.raises(Exception):  # pylint: disable=broad-exception-caught
            bar.open = Decimal("80000")  # type: ignore[misc]


class TestOHLCBarIntervalValidation:
    @pytest.mark.parametrize("interval", [1, 5, 15, 30, 60, 240, 1440, 10080, 21600])
    def test_accepts_kraken_published_intervals(self, interval: int) -> None:
        bar = _make(interval_minutes=interval)
        assert bar.interval_minutes == interval

    @pytest.mark.parametrize("interval", [0, 2, 7, 120, 1000, -1, 99999])
    def test_rejects_unsupported_intervals(self, interval: int) -> None:
        """Anything outside Kraken's published set is a fail-fast error
        at construction — better than a silent mismatch downstream."""
        with pytest.raises(ValueError, match="interval_minutes must be one of"):
            _make(interval_minutes=interval)

    def test_allowed_intervals_set_is_immutable(self) -> None:
        """ALLOWED_INTERVALS is a frozenset so test code can't mutate
        the canonical set and break invariants in another test."""
        assert isinstance(OHLCBar.ALLOWED_INTERVALS, frozenset)


class TestOHLCBarTimestampValidation:
    def test_rejects_naive_opened_at(self) -> None:
        """Same contract as Timestamp: every datetime in the domain is
        tz-aware. Naive timestamps would silently lose timezone context
        when serialized to ISO 8601 for SQLite storage."""
        naive = datetime(2026, 5, 25, 12, 0, 0)
        with pytest.raises(ValueError, match="must be timezone-aware"):
            _make(opened_at=naive)

    def test_normalizes_non_utc_to_utc(self) -> None:
        """An opened_at in CST/EST/PST is accepted but normalized — so
        ORDER BY on the stored ISO string still matches chronological
        order."""
        chicago = timezone(__import__("datetime").timedelta(hours=-5))
        local_noon = datetime(2026, 5, 25, 12, 0, 0, tzinfo=chicago)
        bar = _make(opened_at=local_noon)
        assert bar.opened_at.tzinfo == UTC
        # 12:00 CDT == 17:00 UTC
        assert bar.opened_at.hour == 17


class TestOHLCBarFieldValidation:
    @pytest.mark.parametrize("field", ["open", "high", "low", "close", "vwap", "volume"])
    def test_negative_prices_rejected(self, field: str) -> None:
        with pytest.raises(ValueError):
            _make(**{field: Decimal("-1")})

    def test_zero_prices_allowed(self) -> None:
        """Zero is allowed (some illiquid pairs may report 0 for vwap
        or volume in a quiet bar)."""
        bar = _make(volume=Decimal("0"), count=0)
        assert bar.volume == Decimal("0")
        assert bar.count == 0

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            _make(count=-1)

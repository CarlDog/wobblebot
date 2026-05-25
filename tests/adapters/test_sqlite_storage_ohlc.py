"""Tests for SQLiteStorageAdapter OHLC + latest-observed-at methods.

Slice 2 of the v1.1 cli/observe --backfill feature. Covers:
- save_ohlc_bars idempotency via the UNIQUE (symbol, interval, opened_at)
  constraint
- save_ohlc_bars correct rowcount on partial-overlap re-runs
- get_latest_observed_at returns the max observed_at for a symbol
- get_latest_observed_at returns None for fresh DBs / never-observed symbols
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import OHLCBar, Price, Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_bar(
    *,
    symbol: Symbol = _BTC,
    interval_minutes: int = 1,
    minutes_offset: int = 0,
    close: str = "79000",
) -> OHLCBar:
    return OHLCBar(
        symbol=symbol,
        interval_minutes=interval_minutes,
        opened_at=_T0 + timedelta(minutes=minutes_offset),
        open=Decimal("79000"),
        high=Decimal("79100"),
        low=Decimal("78900"),
        close=Decimal(close),
        vwap=Decimal("79000"),
        volume=Decimal("1.5"),
        count=10,
    )


class TestSaveOHLCBars:
    async def test_empty_input_returns_zero(self, storage: SQLiteStorageAdapter) -> None:
        """No DB round-trip needed; clean 0-row no-op."""
        inserted = await storage.save_ohlc_bars([])
        assert inserted == 0

    async def test_inserts_returned_rowcount(self, storage: SQLiteStorageAdapter) -> None:
        bars = [_make_bar(minutes_offset=i) for i in range(5)]
        inserted = await storage.save_ohlc_bars(bars)
        assert inserted == 5

    async def test_idempotent_rerun_inserts_zero(self, storage: SQLiteStorageAdapter) -> None:
        """The UNIQUE constraint causes INSERT OR IGNORE to silently
        skip already-present rows. The whole point of the backfill
        contract: re-running the same window is a no-op."""
        bars = [_make_bar(minutes_offset=i) for i in range(5)]
        await storage.save_ohlc_bars(bars)
        inserted = await storage.save_ohlc_bars(bars)
        assert inserted == 0

    async def test_partial_overlap_counts_only_new_rows(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Overlapping batches insert only the new bars; the rowcount
        reflects actual writes (the backfill service relies on this to
        report progress accurately)."""
        first = [_make_bar(minutes_offset=i) for i in range(5)]
        await storage.save_ohlc_bars(first)
        second = [_make_bar(minutes_offset=i) for i in range(3, 8)]
        inserted = await storage.save_ohlc_bars(second)
        assert inserted == 3  # offsets 5, 6, 7 are new

    async def test_different_intervals_coexist(self, storage: SQLiteStorageAdapter) -> None:
        """The UNIQUE constraint includes interval_minutes, so 1m + 5m
        bars at the same opened_at are distinct rows."""
        one_min = _make_bar(interval_minutes=1, minutes_offset=0)
        five_min = _make_bar(interval_minutes=5, minutes_offset=0)
        inserted = await storage.save_ohlc_bars([one_min, five_min])
        assert inserted == 2

    async def test_different_symbols_coexist(self, storage: SQLiteStorageAdapter) -> None:
        btc = _make_bar(symbol=_BTC, minutes_offset=0)
        eth = _make_bar(symbol=_ETH, minutes_offset=0)
        inserted = await storage.save_ohlc_bars([btc, eth])
        assert inserted == 2


class TestGetLatestObservedAt:
    async def test_returns_none_for_empty_db(self, storage: SQLiteStorageAdapter) -> None:
        """Fresh DB: never-observed symbol gives None — the gap-fill
        detection treats this as "skip auto-backfill" rather than
        "fetch unbounded history"."""
        latest = await storage.get_latest_observed_at(_BTC)
        assert latest is None

    async def test_returns_max_after_inserts(self, storage: SQLiteStorageAdapter) -> None:
        for offset in (0, 5, 2, 7, 3):  # interleaved, max should be 7
            await storage.save_price_snapshot(
                _BTC,
                Price(amount=Decimal("79000"), currency="USD"),
                Timestamp(dt=_T0 + timedelta(minutes=offset)),
            )
        latest = await storage.get_latest_observed_at(_BTC)
        assert latest == _T0 + timedelta(minutes=7)

    async def test_returns_none_for_other_symbol(self, storage: SQLiteStorageAdapter) -> None:
        """Per-symbol scoping: BTC snapshots don't pollute ETH's window."""
        await storage.save_price_snapshot(
            _BTC,
            Price(amount=Decimal("79000"), currency="USD"),
            Timestamp(dt=_T0),
        )
        latest_eth = await storage.get_latest_observed_at(_ETH)
        assert latest_eth is None

    async def test_returned_datetime_is_utc(self, storage: SQLiteStorageAdapter) -> None:
        """Callers compute `now - latest`; both sides must be tz-aware
        UTC for the arithmetic to be meaningful."""
        await storage.save_price_snapshot(
            _BTC,
            Price(amount=Decimal("79000"), currency="USD"),
            Timestamp(dt=_T0),
        )
        latest = await storage.get_latest_observed_at(_BTC)
        assert latest is not None
        assert latest.tzinfo is UTC

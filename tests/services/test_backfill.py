"""Tests for services/backfill.backfill_range.

Slice 3 of the v1.1 cli/observe --backfill feature. Covers:
- Empty range (since >= until) is a no-op
- Single-page happy path
- Multi-page pagination via Kraken's exclusive ``since`` cursor
- Window upper-bound trimming
- Adapter errors halt the loop and populate ``error``
- Storage errors halt the loop
- Rate limit honored between requests
- Progress callback fires after each successful chunk
- Synthesized snapshots use (opened_at, open) tuple
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.services.backfill import BackfillResult, backfill_range

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_BTC = Symbol(base="BTC", quote="USD")
_T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _make_bar(*, minutes_offset: int, open_price: str = "79000") -> OHLCBar:
    return OHLCBar(
        symbol=_BTC,
        interval_minutes=1,
        opened_at=_T0 + timedelta(minutes=minutes_offset),
        open=Decimal(open_price),
        high=Decimal("79100"),
        low=Decimal("78900"),
        close=Decimal("79050"),
        vwap=Decimal("79000"),
        volume=Decimal("1.5"),
        count=10,
    )


class _StubAdapter:
    """Async adapter that returns pre-canned pages keyed by the ``since``
    timestamp passed in. ``raise_at_call`` makes the Nth call raise."""

    def __init__(
        self,
        pages: list[list[OHLCBar]],
        *,
        raise_at_call: int | None = None,
    ) -> None:
        self._pages = pages
        self._raise_at_call = raise_at_call
        self.call_count = 0
        self.since_history: list[datetime | None] = []

    async def get_ohlc(self, symbol, interval_minutes=1, since=None):  # type: ignore[no-untyped-def]
        _ = symbol, interval_minutes
        self.since_history.append(since)
        self.call_count += 1
        if self._raise_at_call is not None and self.call_count == self._raise_at_call:
            raise ExchangeError(f"synthetic adapter failure on call {self.call_count}")
        if self.call_count > len(self._pages):
            return []
        return self._pages[self.call_count - 1]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestEmptyRange:
    async def test_since_equal_until_short_circuits(self, storage: SQLiteStorageAdapter) -> None:
        adapter = _StubAdapter([])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0,
        )
        assert result.bars_fetched == 0
        assert result.requests_made == 0
        assert adapter.call_count == 0

    async def test_since_after_until_short_circuits(self, storage: SQLiteStorageAdapter) -> None:
        adapter = _StubAdapter([])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0 + timedelta(hours=1),
            until=_T0,
        )
        assert result.requests_made == 0
        assert result.error is None


class TestSinglePageHappyPath:
    async def test_writes_all_bars_and_snapshots(self, storage: SQLiteStorageAdapter) -> None:
        page = [_make_bar(minutes_offset=i) for i in range(1, 6)]
        adapter = _StubAdapter([page, []])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
        )
        assert result.bars_fetched == 5
        assert result.bars_inserted == 5
        assert result.snapshots_inserted == 5
        assert result.error is None
        assert result.last_opened_at == _T0 + timedelta(minutes=5)

    async def test_default_until_is_now(self, storage: SQLiteStorageAdapter) -> None:
        """When `until` is omitted, the service resolves to
        ``datetime.now(UTC)`` at call time. The exact value doesn't
        matter for this assertion; we just confirm the result captures
        a sensible upper bound."""
        adapter = _StubAdapter([])
        before = datetime.now(UTC)
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            rate_limit_seconds=0,
        )
        after = datetime.now(UTC)
        assert before <= result.requested_until <= after


class TestPagination:
    async def test_paginates_via_last_opened_at_cursor(self, storage: SQLiteStorageAdapter) -> None:
        page_a = [_make_bar(minutes_offset=i) for i in range(1, 4)]  # 1, 2, 3
        page_b = [_make_bar(minutes_offset=i) for i in range(4, 7)]  # 4, 5, 6
        adapter = _StubAdapter([page_a, page_b, []])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
        )
        assert result.bars_inserted == 6
        assert result.requests_made >= 2
        # Second call's `since` must be the LAST opened_at of page_a
        # (Kraken's `since` is exclusive — we avoid fetching minute 3 twice).
        assert adapter.since_history[1] == _T0 + timedelta(minutes=3)

    async def test_trims_bars_overshooting_until(self, storage: SQLiteStorageAdapter) -> None:
        page = [_make_bar(minutes_offset=i) for i in range(1, 11)]  # 1..10
        adapter = _StubAdapter([page, []])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=5),
            rate_limit_seconds=0,
        )
        # Only minutes 1..5 fit the window.
        assert result.bars_inserted == 5
        assert result.last_opened_at == _T0 + timedelta(minutes=5)


class TestSnapshotSynthesis:
    async def test_snapshot_uses_opened_at_and_open_price(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The workshopped decision (option A): synthesized snapshot's
        timestamp = bar.opened_at, price = bar.open."""
        page = [_make_bar(minutes_offset=1, open_price="79123.45")]
        adapter = _StubAdapter([page, []])
        await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
        )
        snaps = await storage.get_price_snapshots(symbol=_BTC)
        assert len(snaps) == 1
        assert snaps[0].observed_at.dt == _T0 + timedelta(minutes=1)
        assert snaps[0].price.amount == Decimal("79123.45")


class TestErrorHandling:
    async def test_adapter_error_stops_loop_and_populates_result(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """On ExchangeError, the service stops cleanly, populates
        `error`, and reports the last successful cursor for resume."""
        page_a = [_make_bar(minutes_offset=i) for i in range(1, 4)]
        adapter = _StubAdapter([page_a, []], raise_at_call=2)
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
        )
        assert result.error is not None
        assert "ExchangeError" in result.error
        assert "synthetic adapter failure" in result.error
        # First page's writes still landed; result reports the cursor
        # the operator can resume from.
        assert result.bars_inserted == 3
        assert result.last_opened_at == _T0 + timedelta(minutes=3)

    async def test_storage_error_stops_loop(self) -> None:
        """A StorageError mid-stream halts the run the same way."""

        class _BoomStorage:
            async def save_ohlc_bars(self, bars):  # type: ignore[no-untyped-def]
                raise StorageError("simulated disk full")

            async def save_price_snapshots(self, snapshots):  # type: ignore[no-untyped-def]
                raise AssertionError("should not be reached")

        page = [_make_bar(minutes_offset=1)]
        adapter = _StubAdapter([page, []])
        result = await backfill_range(
            adapter,  # type: ignore[arg-type]
            _BoomStorage(),  # type: ignore[arg-type]
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
        )
        assert result.error is not None
        assert "StorageError" in result.error


class TestRateLimit:
    async def test_sleeps_between_chunks(self, storage: SQLiteStorageAdapter) -> None:
        page_a = [_make_bar(minutes_offset=i) for i in range(1, 4)]
        page_b = [_make_bar(minutes_offset=i) for i in range(4, 7)]
        adapter = _StubAdapter([page_a, page_b, []])
        rate_seconds = 0.05
        before = asyncio.get_event_loop().time()
        await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=rate_seconds,
        )
        elapsed = asyncio.get_event_loop().time() - before
        # At least one sleep between the two successful chunks.
        assert elapsed >= rate_seconds, f"elapsed {elapsed:.3f}s < {rate_seconds}s"


class TestProgressCallback:
    async def test_callback_fires_after_each_chunk(self, storage: SQLiteStorageAdapter) -> None:
        page_a = [_make_bar(minutes_offset=i) for i in range(1, 4)]
        page_b = [_make_bar(minutes_offset=i) for i in range(4, 7)]
        adapter = _StubAdapter([page_a, page_b, []])
        callback_calls: list[BackfillResult] = []

        async def _cb(result: BackfillResult) -> None:
            callback_calls.append(result)

        await backfill_range(
            adapter,  # type: ignore[arg-type]
            storage,
            symbol=_BTC,
            since=_T0,
            until=_T0 + timedelta(minutes=10),
            rate_limit_seconds=0,
            progress_callback=_cb,
        )
        # The callback fires AFTER each successful chunk that has more
        # work queued. The final terminating empty page doesn't fire it.
        assert len(callback_calls) >= 1
        # Running totals are non-decreasing.
        for prior, latest in zip(callback_calls, callback_calls[1:]):
            assert latest.bars_inserted >= prior.bars_inserted

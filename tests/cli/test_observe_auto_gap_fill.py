"""Tests for cli/observe._run_auto_gap_fill.

Slice 5 of the v1.1 backfill feature. Daemon-startup behavior: detect
the gap between the most-recent ``price_snapshots.observed_at`` and
``now``, and fill it via a bounded backfill before the poll loop
starts. Per-symbol decision matrix:

- No history          -> skip silently
- Gap < threshold     -> skip silently (normal restart)
- threshold..max      -> run backfill_range at 1m granularity
- Gap > max           -> log warning; operator must run --backfill manually
- Storage errors      -> log warning; continue to next symbol
- Kraken unreachable  -> log warning per symbol; daemon still proceeds
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import StubOHLCAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.observe import _run_auto_gap_fill
from wobblebot.domain.value_objects import OHLCBar, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed_snapshot(storage: SQLiteStorageAdapter, symbol: Symbol, at: datetime) -> None:
    await storage.save_price_snapshot(
        symbol,
        Price(amount=Decimal("79000"), currency=symbol.quote),
        Timestamp(dt=at),
    )


def _make_bar(*, symbol: Symbol, minutes_offset: int, anchor: datetime) -> OHLCBar:
    return OHLCBar(
        symbol=symbol,
        interval_minutes=1,
        opened_at=anchor + timedelta(minutes=minutes_offset),
        open=Decimal("79000"),
        high=Decimal("79100"),
        low=Decimal("78900"),
        close=Decimal("79050"),
        vwap=Decimal("79000"),
        volume=Decimal("1"),
        count=5,
    )


class TestSkipPaths:
    async def test_empty_symbol_list_returns_immediately(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        adapter = StubOHLCAdapter()
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            storage,
            [],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )
        assert adapter.calls == []

    async def test_no_history_skips_symbol(self, storage: SQLiteStorageAdapter) -> None:
        """A symbol the operator just added to observe.symbols has no
        prior price_snapshots. Auto-gap-fill must NOT silently fetch
        unbounded history -- that's an explicit --backfill decision."""
        adapter = StubOHLCAdapter()
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            storage,
            [_BTC],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )
        assert adapter.calls == []

    async def test_small_gap_skips(self, storage: SQLiteStorageAdapter) -> None:
        """Normal restart (gap < threshold): no backfill."""
        await _seed_snapshot(storage, _BTC, _NOW - timedelta(minutes=2))
        adapter = StubOHLCAdapter()
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            storage,
            [_BTC],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )
        assert adapter.calls == []


class TestMaxBoundary:
    async def test_gap_above_max_logs_warning_and_skips(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A multi-day outage shouldn't trigger automatic hours-long API
        hammering on restart. Operator must explicitly use --backfill."""
        await _seed_snapshot(storage, _BTC, _NOW - timedelta(hours=48))
        adapter = StubOHLCAdapter()
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe"):
            await _run_auto_gap_fill(
                adapter,  # type: ignore[arg-type]
                storage,
                [_BTC],
                threshold_minutes=10.0,
                max_hours=24.0,
                now=_NOW,
            )
        assert adapter.calls == []
        records = [r for r in caplog.records if "exceeds max" in r.getMessage()]
        assert records
        assert getattr(records[0], "symbol", None) == "BTC/USD"


class TestFillingPath:
    async def test_gap_in_window_triggers_backfill(self, storage: SQLiteStorageAdapter) -> None:
        latest = _NOW - timedelta(hours=1)
        await _seed_snapshot(storage, _BTC, latest)
        bars = [_make_bar(symbol=_BTC, minutes_offset=i, anchor=latest) for i in range(1, 6)]
        adapter = StubOHLCAdapter(bars=bars)
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            storage,
            [_BTC],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )
        # Must have called get_ohlc at least once for BTC, with since=latest.
        assert adapter.calls
        first_call = adapter.calls[0]
        assert first_call[0] == _BTC
        assert first_call[1] == 1  # 1-minute interval hardcoded
        assert first_call[2] == latest

    async def test_uses_one_minute_interval_regardless_of_gap_size(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Auto-gap-fill always uses 1m granularity per the slice
        design -- operators wanting different granularity use the
        explicit --backfill --interval flag."""
        await _seed_snapshot(storage, _BTC, _NOW - timedelta(hours=6))
        adapter = StubOHLCAdapter(bars=[])
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            storage,
            [_BTC],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )
        for _symbol, interval, _since in adapter.calls:
            assert interval == 1


class TestPerSymbolIsolation:
    async def test_kraken_unreachable_logs_per_symbol_continues(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Kraken unreachable at startup must NOT prevent the daemon from
        entering its poll loop. The auto-gap-fill function logs and
        returns; the poll loop's own per-tick fault isolation handles
        the ongoing outage."""
        await _seed_snapshot(storage, _BTC, _NOW - timedelta(hours=1))
        await _seed_snapshot(storage, _ETH, _NOW - timedelta(hours=1))
        adapter = StubOHLCAdapter(raise_error=True)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe"):
            # Must NOT raise — the daemon needs to proceed to its poll loop.
            await _run_auto_gap_fill(
                adapter,  # type: ignore[arg-type]
                storage,
                [_BTC, _ETH],
                threshold_minutes=10.0,
                max_hours=24.0,
                now=_NOW,
            )
        # Both symbols attempted; both produced a warning (no symbol's
        # failure short-circuited the other).
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_records) >= 2

    async def test_storage_error_skips_symbol_continues_next(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """If get_latest_observed_at raises for one symbol (unlikely but
        defensive), the loop continues with the others."""

        class _BoomStorage:
            async def get_latest_observed_at(self, symbol):  # type: ignore[no-untyped-def]
                if symbol == _BTC:
                    raise StorageError("simulated read failure on BTC")
                return None  # ETH: treat as no-history -> skip

        adapter = StubOHLCAdapter()
        # Must not raise.
        await _run_auto_gap_fill(
            adapter,  # type: ignore[arg-type]
            _BoomStorage(),  # type: ignore[arg-type]
            [_BTC, _ETH],
            threshold_minutes=10.0,
            max_hours=24.0,
            now=_NOW,
        )

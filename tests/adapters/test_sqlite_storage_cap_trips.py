"""Tests for the cap_trips table (ADR-024 session-loss-cap cool-down)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestRecordAndRead:
    async def test_no_trips_returns_none(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_last_cap_trip_at() is None

    async def test_single_trip_round_trips(self, storage: SQLiteStorageAdapter) -> None:
        ts = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        await storage.record_cap_trip(ts, Decimal("-5.12"))
        result = await storage.get_last_cap_trip_at()
        assert result is not None
        assert result.dt == ts.dt

    async def test_multiple_trips_returns_the_newest(self, storage: SQLiteStorageAdapter) -> None:
        first = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        second = Timestamp(dt=datetime(2026, 6, 6, 9, 0, 0, tzinfo=UTC))
        await storage.record_cap_trip(first, Decimal("-5"))
        await storage.record_cap_trip(second, Decimal("-3"))
        result = await storage.get_last_cap_trip_at()
        assert result is not None
        assert result.dt == second.dt

    async def test_each_call_appends_not_overwrites(self, storage: SQLiteStorageAdapter) -> None:
        """Append-only per ADR-024 decision 1 -- one row per trip, not an
        upserted single row (a full history is cheap and useful)."""
        ts = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        later = Timestamp(dt=ts.dt + timedelta(minutes=1))
        await storage.record_cap_trip(ts, Decimal("-5"))
        await storage.record_cap_trip(later, Decimal("-6"))
        conn = storage._conn  # pylint: disable=protected-access
        assert conn is not None
        async with conn.execute("SELECT COUNT(*) FROM cap_trips") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 2


class TestGetLastCapTrip:
    """The richer read (v1.1 dashboard session card) -- timestamp AND
    the session's PnL at trip time, not just the timestamp."""

    async def test_no_trips_returns_none(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_last_cap_trip() is None

    async def test_single_trip_round_trips_both_fields(self, storage: SQLiteStorageAdapter) -> None:
        ts = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        await storage.record_cap_trip(ts, Decimal("-5.12"))
        result = await storage.get_last_cap_trip()
        assert result is not None
        assert result.tripped_at.dt == ts.dt
        assert result.session_pnl_usd == Decimal("-5.12")

    async def test_multiple_trips_returns_the_newest(self, storage: SQLiteStorageAdapter) -> None:
        first = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        second = Timestamp(dt=datetime(2026, 6, 6, 9, 0, 0, tzinfo=UTC))
        await storage.record_cap_trip(first, Decimal("-5"))
        await storage.record_cap_trip(second, Decimal("-3"))
        result = await storage.get_last_cap_trip()
        assert result is not None
        assert result.tripped_at.dt == second.dt
        assert result.session_pnl_usd == Decimal("-3")

    async def test_agrees_with_get_last_cap_trip_at(self, storage: SQLiteStorageAdapter) -> None:
        """Both reads must describe the same newest row -- they share
        one query under the hood."""
        ts = Timestamp(dt=datetime(2026, 6, 5, 4, 22, 0, tzinfo=UTC))
        await storage.record_cap_trip(ts, Decimal("-5.12"))
        timestamp_only = await storage.get_last_cap_trip_at()
        full_record = await storage.get_last_cap_trip()
        assert full_record is not None
        assert timestamp_only == full_record.tripped_at

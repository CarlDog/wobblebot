"""Tests for the engine_state table round-trip (ADR-030, P3 slice 3).

Mirrors test_sqlite_storage_heartbeats.py: upsert-overwrites semantics,
nullable pre-anchor fields, Decimal/timestamp fidelity, UTC
normalization, and CHECK-constraint enforcement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.value_objects import Symbol

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _row(
    symbol: Symbol = _BTC,
    *,
    paused: bool = False,
    offside: bool = False,
    offside_ticks: int = 0,
    reference_price: Decimal | None = Decimal("50000"),
    anchored_at: datetime | None = _NOW - timedelta(hours=2),
    updated_at: datetime = _NOW,
    offside_since: datetime | None = None,
) -> EngineStateRow:
    return EngineStateRow(
        symbol=symbol,
        paused=paused,
        offside=offside,
        offside_ticks=offside_ticks,
        reference_price=reference_price,
        anchored_at=anchored_at,
        updated_at=updated_at,
        offside_since=offside_since,
    )


class TestSaveAndRead:
    async def test_empty_table_returns_empty_list(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_engine_states() == []

    async def test_round_trip_preserves_every_field(self, storage: SQLiteStorageAdapter) -> None:
        row = _row(
            paused=True,
            offside=True,
            offside_ticks=42,
            reference_price=Decimal("50123.456789"),
        )
        await storage.save_engine_state(row)
        [read] = await storage.get_engine_states()
        assert read == row  # frozen dataclass equality — byte-exact fields

    async def test_upsert_overwrites_not_appends(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_engine_state(_row(paused=False))
        await storage.save_engine_state(_row(paused=True, updated_at=_NOW + timedelta(seconds=5)))
        rows = await storage.get_engine_states()
        assert len(rows) == 1
        assert rows[0].paused is True

    async def test_one_row_per_symbol(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_engine_state(_row(_BTC))
        await storage.save_engine_state(_row(_ETH, offside=True, offside_ticks=7))
        rows = {r.symbol: r for r in await storage.get_engine_states()}
        assert set(rows) == {_BTC, _ETH}
        assert rows[_ETH].offside_ticks == 7

    async def test_pre_anchor_nullables_round_trip(self, storage: SQLiteStorageAdapter) -> None:
        """Before _initialize there is no GridState: reference_price and
        anchored_at persist as honest NULLs, not placeholders."""
        row = _row(reference_price=None, anchored_at=None)
        await storage.save_engine_state(row)
        [read] = await storage.get_engine_states()
        assert read.reference_price is None
        assert read.anchored_at is None

    async def test_non_utc_timestamps_normalized(self, storage: SQLiteStorageAdapter) -> None:
        plus_two = timezone(timedelta(hours=2))
        local = _NOW.astimezone(plus_two)
        await storage.save_engine_state(_row(updated_at=local, anchored_at=local))
        [read] = await storage.get_engine_states()
        assert read.updated_at == _NOW  # same instant
        assert read.updated_at.utcoffset() == timedelta(0)


class TestSchemaConstraints:
    async def test_negative_offside_ticks_rejected(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.ports.exceptions import StorageError

        with pytest.raises(StorageError):
            await storage.save_engine_state(_row(offside_ticks=-1))


class TestOffsideSince:
    """The wall-clock start of the current offside episode (Group 3).

    NULL is a first-class value here, not a missing one: it means the
    episode began before anything observed its start. Stamping a boot
    time instead would assert a confident wrong date.
    """

    async def test_round_trips(self, storage: SQLiteStorageAdapter) -> None:
        since = _NOW - timedelta(days=15)
        await storage.save_engine_state(_row(offside=True, offside_ticks=9, offside_since=since))
        [read] = await storage.get_engine_states()
        assert read.offside_since == since

    async def test_null_round_trips_as_null(self, storage: SQLiteStorageAdapter) -> None:
        """An offside row with no recorded start stays unknown, and must
        not acquire one on the way through storage."""
        await storage.save_engine_state(_row(offside=True, offside_ticks=9))
        [read] = await storage.get_engine_states()
        assert read.offside_since is None

    async def test_non_utc_normalized(self, storage: SQLiteStorageAdapter) -> None:
        plus_two = timezone(timedelta(hours=2))
        await storage.save_engine_state(
            _row(offside=True, offside_since=(_NOW - timedelta(days=1)).astimezone(plus_two))
        )
        [read] = await storage.get_engine_states()
        assert read.offside_since == _NOW - timedelta(days=1)
        assert read.offside_since is not None
        assert read.offside_since.utcoffset() == timedelta(0)

    async def test_cleared_by_a_later_upsert(self, storage: SQLiteStorageAdapter) -> None:
        """Coming back onside must actually erase the stamp, not leave the
        previous episode's start behind for the next one to inherit."""
        await storage.save_engine_state(
            _row(offside=True, offside_ticks=9, offside_since=_NOW - timedelta(hours=3))
        )
        await storage.save_engine_state(_row(offside=False, offside_ticks=0))
        [read] = await storage.get_engine_states()
        assert read.offside_since is None


class TestCorruptValuesDegradeTheRowNotTheRestore:
    """A visibility column must never cost a pause restore.

    cli/live replays operator pauses from these rows at boot, so a row
    dropped over an unreadable duration silently resumes real trading on
    a symbol someone deliberately stopped.
    """

    async def _corrupt(self, storage: SQLiteStorageAdapter, column: str, value: object) -> None:
        conn = storage._require_conn()  # pylint: disable=protected-access
        await conn.execute(f"UPDATE engine_state SET {column} = ?", (value,))  # nosec
        await conn.commit()

    async def test_unparseable_offside_since_degrades_to_none(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.save_engine_state(_row(paused=True, offside=True, offside_ticks=9))
        await self._corrupt(storage, "offside_since", "not-a-timestamp")
        [read] = await storage.get_engine_states()
        assert read.offside_since is None
        assert read.paused is True  # the row, and the pause, survived

    async def test_unparseable_offside_ticks_degrades_to_zero(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.save_engine_state(_row(paused=True, offside=True, offside_ticks=9))
        await self._corrupt(storage, "offside_ticks", "garbage")
        [read] = await storage.get_engine_states()
        assert read.offside_ticks == 0
        assert read.paused is True  # the row, and the pause, survived

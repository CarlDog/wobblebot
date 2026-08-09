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
) -> EngineStateRow:
    return EngineStateRow(
        symbol=symbol,
        paused=paused,
        offside=offside,
        offside_ticks=offside_ticks,
        reference_price=reference_price,
        anchored_at=anchored_at,
        updated_at=updated_at,
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

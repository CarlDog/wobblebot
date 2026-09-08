"""Starvation diagnostics must never discard a persisted operator pause."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tests.adapters.test_sqlite_engine_state import storage as storage
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _row():
    return EngineStateRow(
        symbol=Symbol(base="BTC", quote="USD"),
        paused=True,
        offside=True,
        offside_ticks=123,
        reference_price=None,
        anchored_at=None,
        offside_since=datetime(2026, 8, 19, tzinfo=UTC),
        updated_at=datetime.now(UTC),
        starved_ticks=60,
        starved_target=6,
        starved_refusals=3,
        starved_sells_deferred=3,
        starved_reasons={"insufficient_balance": 3},
    )


async def test_insert_upsert_and_clear_round_trip_every_field(storage):
    row = _row()
    await storage.save_engine_state(row)
    assert await storage.get_engine_states() == [row]
    changed = replace(
        row,
        starved_ticks=120,
        starved_target=5,
        starved_refusals=4,
        starved_sells_deferred=1,
        starved_reasons={"exchange_error": 4},
        updated_at=datetime.now(UTC),
    )
    await storage.save_engine_state(changed)
    assert await storage.get_engine_states() == [changed]
    cleared = replace(
        changed,
        starved_ticks=0,
        starved_target=0,
        starved_refusals=0,
        starved_sells_deferred=0,
        starved_reasons={},
    )
    await storage.save_engine_state(cleared)
    assert await storage.get_engine_states() == [cleared]


@pytest.mark.parametrize(
    "column,value",
    [
        ("starved_ticks", "bad"),
        ("starved_ticks", -1),
        ("starved_target", 0.5),
        ("starved_refusals", "bad"),
        ("starved_sells_deferred", "bad"),
        ("starved_reasons", "{bad"),
        ("starved_reasons", "[]"),
        ("starved_reasons", '{"insufficient_balance": true}'),
        ("starved_reasons", '{"insufficient_balance": -3}'),
        ("starved_reasons", '{"insufficient_balance": 3, "broken": "x"}'),
        ("starved_reasons", '{"insufficient_balance": 2}'),
        ("starved_updated_at", "old writer"),
    ],
)
async def test_corrupt_diagnostics_degrade_together_but_preserve_pause(storage, column, value):
    original = _row()
    await storage.save_engine_state(original)
    conn = storage._require_conn()
    await conn.execute("PRAGMA ignore_check_constraints=ON")
    await conn.execute(f"UPDATE engine_state SET {column}=?", (value,))
    await conn.commit()
    [read] = await storage.get_engine_states()
    assert read.paused and read.offside
    assert read.offside_ticks == 123 and read.offside_since == original.offside_since
    assert (
        read.starved_ticks,
        read.starved_target,
        read.starved_refusals,
        read.starved_sells_deferred,
        read.starved_reasons,
    ) == (0, 0, 0, 0, {})


async def test_invalid_encoding_uses_the_port_error(storage):
    with pytest.raises(StorageError, match="encode engine-state"):
        await storage.save_engine_state(replace(_row(), starved_reasons={"bad": object()}))

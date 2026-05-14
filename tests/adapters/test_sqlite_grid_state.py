"""SQLiteStorageAdapter tests for the GridState persistence methods."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.grid import GridState
from wobblebot.domain.value_objects import Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _state(symbol: Symbol, ref: str = "50000") -> GridState:
    return GridState(
        symbol=symbol,
        reference_price=Decimal(ref),
        spacing_percentage=Decimal("1.0"),
        levels_above=5,
        levels_below=5,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


async def test_get_grid_state_returns_none_for_unknown_symbol(
    storage: SQLiteStorageAdapter,
) -> None:
    assert await storage.get_grid_state(Symbol(base="BTC", quote="USD")) is None


async def test_save_then_get_round_trip(storage: SQLiteStorageAdapter) -> None:
    sym = Symbol(base="BTC", quote="USD")
    original = _state(sym)
    await storage.save_grid_state(original)

    loaded = await storage.get_grid_state(sym)
    assert loaded is not None
    assert loaded.symbol == sym
    assert loaded.reference_price == Decimal("50000")
    assert loaded.spacing_percentage == Decimal("1.0")
    assert loaded.levels_above == 5
    assert loaded.levels_below == 5
    # Timestamp round-trips through ISO 8601 UTC
    assert loaded.created_at.dt == original.created_at.dt


async def test_save_is_idempotent_per_symbol(storage: SQLiteStorageAdapter) -> None:
    sym = Symbol(base="BTC", quote="USD")
    await storage.save_grid_state(_state(sym, ref="50000"))
    await storage.save_grid_state(_state(sym, ref="60000"))

    loaded = await storage.get_grid_state(sym)
    assert loaded is not None
    # Last write wins; only one row exists for this symbol
    assert loaded.reference_price == Decimal("60000")


async def test_distinct_symbols_have_independent_rows(
    storage: SQLiteStorageAdapter,
) -> None:
    btc = Symbol(base="BTC", quote="USD")
    eth = Symbol(base="ETH", quote="USD")
    await storage.save_grid_state(_state(btc, ref="50000"))
    await storage.save_grid_state(_state(eth, ref="3000"))

    btc_state = await storage.get_grid_state(btc)
    eth_state = await storage.get_grid_state(eth)
    assert btc_state is not None and btc_state.reference_price == Decimal("50000")
    assert eth_state is not None and eth_state.reference_price == Decimal("3000")


async def test_decimal_precision_preserved(storage: SQLiteStorageAdapter) -> None:
    sym = Symbol(base="BTC", quote="USD")
    state = GridState(
        symbol=sym,
        reference_price=Decimal("12345.6789012345"),
        spacing_percentage=Decimal("0.123456789"),
        levels_above=5,
        levels_below=5,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    await storage.save_grid_state(state)
    loaded = await storage.get_grid_state(sym)
    assert loaded is not None
    assert loaded.reference_price == Decimal("12345.6789012345")
    assert loaded.spacing_percentage == Decimal("0.123456789")

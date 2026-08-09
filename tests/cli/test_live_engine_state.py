"""Tests for cli/live's engine_state publishing (ADR-030, P3 slice 3).

Pins the load-bearing choices: state comes from the ENGINE accessors
(a paused symbol reports paused even though its StepResult.offside is
False), a failed grid-state read degrades reference_price/anchored_at
to None without dropping the row, and an unwired operator_db is a
total no-op.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _emit_engine_states
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000")},
        starting_prices={BTC_USD: Decimal("50000"), ETH_USD: Decimal("3000")},
    )


class TestEmitEngineStates:
    async def test_one_row_per_symbol_with_anchor_data(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # anchors at 50000

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.symbol == BTC_USD
        assert row.paused is False
        assert row.offside is False
        assert row.offside_ticks == 0
        assert row.reference_price == Decimal("50000")
        assert row.anchored_at is not None

    async def test_paused_symbol_reports_paused(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The trap this design avoids: a paused symbol's StepResult
        carries offside=False and no pause flag — state must come from
        the engine accessors."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        engine.pause_symbol(BTC_USD)
        await engine.step(BTC_USD)  # skipped_paused

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.paused is True

    async def test_offside_symbol_reports_ticks(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("48000"))  # below the band
        await engine.step(BTC_USD)

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.offside is True
        assert row.offside_ticks >= 1

    async def test_pre_anchor_symbol_writes_nullable_row(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A symbol that never stepped still gets a row (paused=False,
        offside=False) with honest None anchor fields."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())

        await _emit_engine_states(engine, [ETH_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.symbol == ETH_USD
        assert row.reference_price is None
        assert row.anchored_at is None

    async def test_unwired_operator_db_is_noop(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        # Must not raise, must not read grid state.
        await _emit_engine_states(engine, [BTC_USD], storage, None)

    async def test_grid_state_read_failure_degrades_not_drops(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A broken live.db read must still publish paused/offside —
        the load-bearing visibility — with anchor fields None."""
        from wobblebot.ports.exceptions import StorageError

        class _BrokenGridStateStorage(SQLiteStorageAdapter):
            async def get_grid_state(self, symbol):  # type: ignore[no-untyped-def]
                raise StorageError("live.db unreadable")

        broken = _BrokenGridStateStorage(":memory:")
        await broken.connect()
        try:
            engine = GridEngine(_exchange(), broken, _grid_config(), _safety_config())
            engine.pause_symbol(BTC_USD)

            await _emit_engine_states(engine, [BTC_USD], broken, operator_storage)

            [row] = await operator_storage.get_engine_states()
            assert row.paused is True
            assert row.reference_price is None
        finally:
            await broken.close()

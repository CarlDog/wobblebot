"""Tests for cli/live's ADR-024 cool-down recording in ``_run_loop``.

``_run_loop`` is otherwise integration territory (see
``test_live_dead_mans_switch.py``); this targets only the record-on-trip
behavior added for ADR-024: a session that exits on the loss cap
(exit_code=1) records a cap_trips row, a clean exit does not.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _run_loop
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


class _LossOnSecondBalanceCheck(MockExchangeAdapter):
    """Reports a reduced USD balance from the second ``get_balances()``
    call onward, deterministically simulating a mark-to-market loss
    between session-start (call 1) and the first tick's post-tick
    loss-cap check (call 2+) -- without depending on fee arithmetic."""

    def __init__(self, *args: object, loss_usd: Decimal, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._loss_usd = loss_usd
        self._call_count = 0

    async def get_balances(self):  # type: ignore[no-untyped-def]
        balances = await super().get_balances()
        self._call_count += 1
        if self._call_count > 1:
            balances = [
                (
                    b.model_copy(update={"total": b.total - self._loss_usd, "available": b.total})
                    if b.asset == "USD"
                    else b
                )
                for b in balances
            ]
        return balances


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _live(**overrides: object) -> LiveConfig:
    return LiveConfig(symbols=[BTC_USD], **overrides)  # type: ignore[arg-type]


class TestCapTripRecording:
    async def test_loss_cap_trip_records_cap_trip(self, storage: SQLiteStorageAdapter) -> None:
        exch = _LossOnSecondBalanceCheck(
            starting_balances={"USD": Decimal("1000")},
            starting_prices={BTC_USD: Decimal("50000")},
            loss_usd=Decimal("10"),
        )
        engine = GridEngine(exch, storage, grid_config(), safety_config())

        code = await _run_loop(
            exch, engine, _live(max_session_loss_usd=Decimal("5")), storage, asyncio.Event()
        )

        assert code == 1
        last_trip = await storage.get_last_cap_trip_at()
        assert last_trip is not None

    async def test_clean_exit_does_not_record_cap_trip(self, storage: SQLiteStorageAdapter) -> None:
        exch = MockExchangeAdapter(
            starting_balances={"USD": Decimal("1000")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, grid_config(), safety_config())
        engine.request_stop()  # exit cleanly before any loss could accrue

        code = await _run_loop(exch, engine, _live(), storage, asyncio.Event())

        assert code == 0
        assert await storage.get_last_cap_trip_at() is None

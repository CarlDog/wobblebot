"""Tests for cli/live's dead man's switch wiring in ``_run_loop`` (ADR-021).

``_run_loop`` is otherwise integration territory; these target only the
switch logic added for ADR-021: arm/pet on every tick, and disarm in the
``finally`` *only* on a confirmed-clean cancel (leave it armed when our
own cancellation failed, so Kraken's timer is the backstop). Driven with
a ``MockExchangeAdapter`` subclass that records the call sequence.

``engine.request_stop()`` is pre-set so the loop pets the switch once,
then breaks before placing any orders — keeping the test deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _run_loop
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


class _RecordingExchange(MockExchangeAdapter):
    """Mock that records the dead-man's-switch call sequence and can be
    told to fail every cancel (to exercise the leave-armed path)."""

    def __init__(self, *args: object, fail_cancel: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dms_calls: list[int] = []
        self._fail_cancel = fail_cancel

    async def set_dead_mans_switch(self, timeout_seconds: int) -> None:
        self.dms_calls.append(timeout_seconds)
        await super().set_dead_mans_switch(timeout_seconds)

    async def cancel_order(self, order: Order) -> Order:
        if self._fail_cancel:
            raise ExchangeError("simulated cancel failure")
        return await super().cancel_order(order)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _live(**overrides: object) -> LiveConfig:
    return LiveConfig(symbols=[BTC_USD], **overrides)  # type: ignore[arg-type]


def _engine(exch: MockExchangeAdapter, storage: SQLiteStorageAdapter) -> GridEngine:
    return GridEngine(exch, storage, grid_config(), safety_config())


async def _place_resting_buy(exch: MockExchangeAdapter) -> None:
    """Place a BUY well below market so it rests (does not fill)."""
    order = Order(
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("40000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    await exch.place_order(order)


async def test_armed_each_tick_and_disarmed_on_clean_exit(
    storage: SQLiteStorageAdapter,
) -> None:
    exch = _RecordingExchange(
        starting_balances={"USD": Decimal("1000")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = _engine(exch, storage)
    engine.request_stop()  # pet once, then break before placing orders

    code = await _run_loop(exch, engine, _live(), storage, asyncio.Event())

    assert code == 0
    # Pet with the configured timeout, then disarmed (0) on the clean exit.
    assert exch.dms_calls == [60, 0]


async def test_left_armed_when_cancel_fails(storage: SQLiteStorageAdapter) -> None:
    exch = _RecordingExchange(
        starting_balances={"USD": Decimal("1000")},
        starting_prices={BTC_USD: Decimal("50000")},
        fail_cancel=True,
    )
    await _place_resting_buy(exch)  # the finally will try (and fail) to cancel it
    engine = _engine(exch, storage)
    engine.request_stop()

    await _run_loop(exch, engine, _live(), storage, asyncio.Event())

    # Pet happened (60); NOT disarmed (no trailing 0) because the cancel
    # failed — Kraken's timer stays armed as the backstop.
    assert exch.dms_calls == [60]


async def test_disabled_switch_is_never_touched(storage: SQLiteStorageAdapter) -> None:
    exch = _RecordingExchange(
        starting_balances={"USD": Decimal("1000")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = _engine(exch, storage)
    engine.request_stop()

    await _run_loop(exch, engine, _live(dead_mans_switch_seconds=None), storage, asyncio.Event())

    assert exch.dms_calls == []

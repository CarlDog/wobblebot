"""ADR-046 in cli/live: when the engine gives up recovering a confirmed
fill's trade rows, the operator gets a critical page naming the order,
and a restart resumes a sweep the previous process left behind.

Drives ``_run_one_tick`` against the mock exchange with the trade
withheld (Kraken's 2026-09-10 shape) and the attempt bound patched to
one, so the second tick abandons.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _AuthEscalation, _run_one_tick
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.services import grid_engine as grid_engine_module
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )


def _live() -> LiveConfig:
    return LiveConfig(symbols=[BTC_USD])


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _fill_with_hidden_trade(
    exchange: MockExchangeAdapter, engine: GridEngine, storage: SQLiteStorageAdapter
) -> str:
    await engine.step(BTC_USD)
    buy = next(
        o
        for o in await storage.get_open_orders(symbol=BTC_USD)
        if o.side is OrderSide.BUY and o.price.amount == Decimal("49500")
    )
    assert buy.exchange_id
    exchange.withhold_trades(buy.exchange_id)
    exchange.set_price(BTC_USD, Decimal("49400"))
    return buy.exchange_id


async def test_abandoned_recovery_pages_the_operator_with_the_order_id(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)
    escalation = _AuthEscalation()

    # Tick 1 detects the fill and writes the marker; tick 2's sweep hits the bound.
    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )
    await _run_one_tick(
        exchange, engine, _live(), 2, Decimal("100000"), notifier, escalation=escalation
    )

    rows = await storage.get_notifications()
    pages = [r.notification for r in rows if r.notification.title.startswith("Fill recorded")]
    assert len(pages) == 1
    page = pages[0]
    assert page.level == "critical"
    assert exchange_id in page.message
    assert "reconcile_trade_history" in page.message
    assert page.context["exchange_ids"] == [exchange_id]
    assert page.context["reason"] == "fill_trades_unrecovered"
    # The ordinary fill notification from tick 1 is still there too.
    assert any(r.notification.title.startswith("Fills:") for r in rows)


async def test_successful_recovery_does_not_page(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    exchange_id = await _fill_with_hidden_trade(exchange, engine, storage)
    escalation = _AuthEscalation()

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )
    exchange.release_trades(exchange_id)
    await _run_one_tick(
        exchange, engine, _live(), 2, Decimal("100000"), notifier, escalation=escalation
    )

    rows = await storage.get_notifications()
    assert not any(r.notification.title.startswith("Fill recorded") for r in rows)
    assert len(await storage.get_trades(symbol=BTC_USD)) == 1
    assert await storage.get_pending_fill_trades(BTC_USD) == []

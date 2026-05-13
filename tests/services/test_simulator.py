"""Integration test for the Phase 1 simulator.

Composes MockExchangeAdapter + SQLiteStorageAdapter + the simulator
service. Proves the hex layers wire up end-to-end before any real
exchange code lands in Phase 2.

Runs against ``:memory:`` SQLite, so still fast — not a slow test, but
deliberately marked ``integration`` to flag that it's exercising
multiple modules together rather than one unit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.services.simulator import run_buy_dip_sell_rebound_cycle

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def test_full_buy_dip_sell_rebound_cycle(storage: SQLiteStorageAdapter) -> None:
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("10000")},
        starting_prices={BTC_USD: Decimal("50000")},
    )

    result = await run_buy_dip_sell_rebound_cycle(
        exchange,
        storage,
        symbol=BTC_USD,
        buy_price=Decimal("48000"),
        sell_price=Decimal("52000"),
        amount=Decimal("0.05"),
        price_walk=[
            Decimal("49000"),  # above buy limit - no fill
            Decimal("47500"),  # below buy limit - buy fills, sell placed
            Decimal("51000"),  # below sell limit - no fill
            Decimal("52500"),  # above sell limit - sell fills
        ],
    )

    # Summary reflects the cycle
    assert result.orders_placed == 2
    assert result.trades_executed == 2

    # Both trades persisted
    trades = await storage.get_trades()
    assert len(trades) == 2
    # Newest first - sell trade is the most recent
    assert trades[0].side is OrderSide.SELL
    assert trades[1].side is OrderSide.BUY

    # Both orders persisted with closed status
    assert await storage.get_open_orders() == []

    # Final balance snapshot persisted and reads back
    snapshot = await storage.get_latest_balance_snapshot()
    by_asset = {b.asset: b for b in snapshot}

    # USD math:
    #   start         = 10000
    #   - buy cost    = 0.05 * 48000 = 2400
    #   - buy fee     = 2400 * 0.0026 = 6.24
    #   + sell cost   = 0.05 * 52000 = 2600
    #   - sell fee    = 2600 * 0.0026 = 6.76
    # end = 10000 - 2400 - 6.24 + 2600 - 6.76 = 10187
    assert by_asset["USD"].total == Decimal("10187")
    # All BTC bought was later sold
    assert by_asset["BTC"].total == Decimal("0")


async def test_no_fill_no_trades_persisted(storage: SQLiteStorageAdapter) -> None:
    """If the price walk never crosses, only the placed buy is recorded."""
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("10000")},
        starting_prices={BTC_USD: Decimal("50000")},
    )

    result = await run_buy_dip_sell_rebound_cycle(
        exchange,
        storage,
        symbol=BTC_USD,
        buy_price=Decimal("40000"),  # far below market
        sell_price=Decimal("55000"),
        amount=Decimal("0.05"),
        price_walk=[Decimal("50500"), Decimal("49500")],  # never crosses
    )

    assert result.orders_placed == 1  # just the buy
    assert result.trades_executed == 0
    assert await storage.get_trades() == []

    # The buy is still open
    open_orders = await storage.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].status == "open"

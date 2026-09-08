"""Cap regressions for counter quantities independent of configured order size."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.grid import GridLevel, GridState
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
BTC = Symbol(base="BTC", quote="USD")
USD_CAPS = (
    "max_per_coin_exposure_usd",
    "max_total_exposure_usd",
    "max_daily_spend_usd",
    "max_per_coin_inventory_usd",
    "max_total_inventory_usd",
)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


def exchange_at(price: str = "50000") -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC: Decimal(price)},
    )


def order_at(side: OrderSide, price: str, amount: str) -> Order:
    return Order(
        symbol=BTC,
        side=side,
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal(amount), asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


@pytest.mark.parametrize("configured_size", ["2", "20"])
@pytest.mark.parametrize("notional", ["4", "5", "6"])
@pytest.mark.parametrize(
    ("side", "cap_name"),
    [(OrderSide.BUY, cap) for cap in USD_CAPS] + [(OrderSide.SELL, cap) for cap in USD_CAPS[:2]],
)
async def test_counter_uses_actual_remaining_headroom(
    storage: SQLiteStorageAdapter,
    configured_size: str,
    notional: str,
    side: OrderSide,
    cap_name: str,
) -> None:
    """A $2 open BUY leaves $5 of a $7 cap, independent of today's size."""
    existing = order_at(OrderSide.BUY, "40000", "0.00005")
    existing.status = "open"
    await storage.save_order(existing)
    safety = safety_config(sell_guard_enabled=False).model_copy(update={cap_name: Decimal("7")})
    grid = grid_config(order_size=configured_size)
    exchange = exchange_at("60000" if side is OrderSide.BUY else "40000")
    engine = GridEngine(exchange, storage, grid, safety)
    amount = Amount(value=Decimal(notional) / Decimal("50000"), asset="BTC")

    outcome, reason = await engine._try_place(
        BTC,
        GridLevel(index=0, side=side, price=Decimal("50000")),
        grid.for_coin("BTC"),
        amount=amount,
    )

    orders = [order for order in await storage.get_orders() if order.id != existing.id]
    if Decimal(notional) > 5:
        assert (outcome, reason) == ("refused", cap_name)
        assert orders == []
        assert await exchange.get_open_orders() == []
    else:
        assert (outcome, reason) == ("placed", "")
        assert len(orders) == 1
        assert orders[0].amount == amount
        assert orders[0].price.amount * orders[0].amount.value == Decimal(notional)


@pytest.mark.parametrize("cap_name", USD_CAPS[2:])
async def test_buy_only_caps_do_not_block_sell_counter(
    storage: SQLiteStorageAdapter, cap_name: str
) -> None:
    safety = safety_config(sell_guard_enabled=False).model_copy(update={cap_name: Decimal("1")})
    grid = grid_config(order_size="2")
    engine = GridEngine(exchange_at(), storage, grid, safety)
    outcome, reason = await engine._try_place(
        BTC,
        GridLevel(index=1, side=OrderSide.SELL, price=Decimal("60000")),
        grid.for_coin("BTC"),
        amount=Amount(value=Decimal("0.0002"), asset="BTC"),
    )
    assert (outcome, reason) == ("placed", "")
    assert len(await storage.get_orders()) == 1


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
async def test_small_counter_still_consumes_an_order_slot(
    storage: SQLiteStorageAdapter, side: OrderSide
) -> None:
    existing = order_at(OrderSide.BUY, "40000", "0.00005")
    existing.status = "open"
    await storage.save_order(existing)
    grid = grid_config(order_size="20")
    engine = GridEngine(exchange_at(), storage, grid, safety_config(max_orders=1))
    assert await engine._try_place(
        BTC,
        GridLevel(index=0, side=side, price=Decimal("50000")),
        grid.for_coin("BTC"),
        amount=Amount(value=Decimal("0.00002"), asset="BTC"),
    ) == ("refused", "max_orders_per_coin")


@pytest.mark.parametrize("recovered", [False, True], ids=["tick", "recovery"])
@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("filled_amount", ["0.0002", "0.0001"], ids=["full", "partial"])
@pytest.mark.parametrize("configured_size", ["2", "20"])
async def test_fill_and_recovery_paths_check_counter_notional(
    storage: SQLiteStorageAdapter,
    recovered: bool,
    side: OrderSide,
    filled_amount: str,
    configured_size: str,
) -> None:
    """Old fills counter at $50k: full $10 is blocked; partial $5 fits exactly."""
    fill_price = "49500" if side is OrderSide.BUY else "50500"
    exchange = exchange_at()
    original = await exchange.place_order(order_at(side, fill_price, "0.0002"))
    await storage.save_order(original)
    # Keep a $1 order on the book so refusing a counter cannot trigger re-layout.
    sentinel = await exchange.place_order(order_at(OrderSide.BUY, "40000", "0.000025"))
    await storage.save_order(sentinel)
    exchange.inject_partial_cancel(original, filled_amount=Decimal(filled_amount))
    if recovered:
        original.status = "canceled"
        original.filled_amount = Decimal(filled_amount)
        await storage.save_order(original)
    await storage.save_grid_state(
        GridState(
            symbol=BTC,
            reference_price=Decimal("50000"),
            spacing_percentage=Decimal("1"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    exchange.set_price(BTC, Decimal(fill_price))
    engine = GridEngine(
        exchange,
        storage,
        grid_config(order_size=configured_size),
        safety_config(max_per_coin="6", sell_guard_enabled=False),
        pending_counters=[original.id] if recovered else None,
    )

    result = await engine.step(BTC)

    counters = [
        order for order in await storage.get_orders() if order.id not in {original.id, sentinel.id}
    ]
    if filled_amount == "0.0002":
        assert result.refusals == 1
        assert counters == []
        if recovered:
            assert original.id in engine._pending_counter_ids
            again = await engine.step(BTC)
            assert again.refusals == 1
            assert original.id in engine._pending_counter_ids
    else:
        assert result.refusals == 0
        assert len(counters) == 1
        assert counters[0].side is not side
        assert counters[0].price.amount == Decimal("50000")
        assert counters[0].amount.value == Decimal(filled_amount)
        assert counters[0].price.amount * counters[0].amount.value == Decimal("5")
        if recovered:
            assert original.id not in engine._pending_counter_ids

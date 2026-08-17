"""Shared exposure/daily-spend math (extracted 2026-08-10).

The reason this module exists is drift: `GridEngine._check_safety`
enforces the caps and the SummaryBuilder now *reports* them to the risk
advisor. Two implementations would eventually disagree, and the advisor
would reason about headroom the engine denies. These tests pin the rule
that is easiest to get wrong when re-implementing — the committed-funds
filter on daily spend, which came from a real 2026-05-22 soak incident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.exposure import (
    SPEND_COMMITTED_STATUSES,
    buy_notional_usd,
    coin_exposure_usd,
    coin_inventory_cost_usd,
    daily_spend_usd,
    notional_usd,
    total_exposure_usd,
    total_inventory_cost_usd,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _order(
    *,
    symbol: str = "BTC/USD",
    side: OrderSide = OrderSide.BUY,
    price: str = "100",
    amount: str = "0.1",
    status: str = "open",
    created: datetime = _NOW,
) -> Order:
    return Order(
        id=uuid4(),
        symbol=Symbol.from_string(symbol),
        side=side,
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal(amount), asset=Symbol.from_string(symbol).base),
        status=status,
        created_at=Timestamp(dt=created),
    )


class TestNotional:
    def test_sums_price_times_amount(self) -> None:
        orders = [_order(price="100", amount="0.5"), _order(price="200", amount="0.25")]
        assert notional_usd(orders) == Decimal("100")

    def test_empty_is_zero_not_error(self) -> None:
        assert notional_usd([]) == Decimal("0")


@pytest.mark.asyncio
class TestExposure:
    async def test_coin_exposure_is_symbol_scoped(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_order(_order(symbol="BTC/USD", price="100", amount="0.3"))
        await storage.save_order(_order(symbol="ETH/USD", price="100", amount="0.7"))
        assert await coin_exposure_usd(storage, Symbol.from_string("BTC/USD")) == Decimal("30")

    async def test_total_exposure_spans_symbols(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_order(_order(symbol="BTC/USD", price="100", amount="0.3"))
        await storage.save_order(_order(symbol="ETH/USD", price="100", amount="0.7"))
        assert await total_exposure_usd(storage) == Decimal("100")

    async def test_closed_orders_are_not_open_exposure(self, storage: SQLiteStorageAdapter) -> None:
        """Exposure is what is ON THE BOOK; a filled order is no longer
        standing capital."""
        await storage.save_order(_order(status="open", price="100", amount="0.4"))
        await storage.save_order(_order(status="closed", price="100", amount="0.6"))
        assert await total_exposure_usd(storage) == Decimal("40")


@pytest.mark.asyncio
class TestDailySpend:
    """The soak-learned rule. Canceled/expired BUYs never moved money."""

    @pytest.mark.parametrize("status", sorted(SPEND_COMMITTED_STATUSES))
    async def test_committed_statuses_count(
        self, storage: SQLiteStorageAdapter, status: str
    ) -> None:
        await storage.save_order(_order(status=status, price="100", amount="0.5"))
        assert await daily_spend_usd(storage, now=_NOW) == Decimal("50")

    @pytest.mark.parametrize("status", ["canceled", "expired"])
    async def test_uncommitted_statuses_are_excluded(
        self, storage: SQLiteStorageAdapter, status: str
    ) -> None:
        """The 2026-05-22 incident: 11 canceled BUYs ate the day's
        headroom and blocked legitimate placements at $110 of a $100
        cap. Counting them is the bug."""
        await storage.save_order(_order(status=status, price="100", amount="0.9"))
        assert await daily_spend_usd(storage, now=_NOW) == Decimal("0")

    async def test_sells_do_not_count_as_spend(self, storage: SQLiteStorageAdapter) -> None:
        """A SELL returns capital; only BUYs commit it."""
        await storage.save_order(_order(side=OrderSide.SELL, price="100", amount="0.8"))
        assert await daily_spend_usd(storage, now=_NOW) == Decimal("0")

    async def test_yesterdays_buys_do_not_count(self, storage: SQLiteStorageAdapter) -> None:
        """The window is midnight UTC, so the cap resets daily."""
        await storage.save_order(
            _order(price="100", amount="0.7", created=_NOW - timedelta(days=1))
        )
        assert await daily_spend_usd(storage, now=_NOW) == Decimal("0")

    async def test_boundary_just_after_midnight_counts(self, storage: SQLiteStorageAdapter) -> None:
        midnight = datetime(2026, 8, 10, 0, 0, 1, tzinfo=UTC)
        await storage.save_order(_order(price="100", amount="0.2", created=midnight))
        assert await daily_spend_usd(storage, now=_NOW) == Decimal("20")


def _trade(
    *,
    symbol: str = "BTC/USD",
    side: OrderSide = OrderSide.BUY,
    price: str = "50000",
    amount: str = "0.0002",
    fee: str = "0",
    minutes_ago: int = 0,
) -> Trade:
    sym = Symbol.from_string(symbol)
    px = Decimal(price)
    amt = Decimal(amount)
    return Trade(
        id=f"T-{uuid4()}",
        order_id=f"O-{uuid4()}",
        symbol=sym,
        side=side,
        price=Price(amount=px, currency="USD"),
        amount=Amount(value=amt, asset=sym.base),
        fee=Decimal(fee),
        cost=px * amt,
        executed_at=Timestamp(dt=_NOW - timedelta(minutes=minutes_ago)),
    )


class TestBuyNotional:
    def test_filters_to_buys_only(self) -> None:
        orders = [
            _order(side=OrderSide.BUY, price="100", amount="0.1"),
            _order(side=OrderSide.SELL, price="200", amount="0.1"),
        ]
        assert buy_notional_usd(orders) == Decimal("10")


class TestInventoryCost:
    """ADR-039 — inventory at average cost basis, via the sell guard's replay."""

    @pytest.mark.asyncio
    async def test_buys_accumulate_at_cost_with_fees(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(price="50000", amount="0.0002", fee="0.04", minutes_ago=2))
        await storage.save_trade(_trade(price="60000", amount="0.0002", fee="0.05", minutes_ago=1))
        # replay capitalizes fees: (10 + 0.04) + (12 + 0.05) = 22.09
        inv = await coin_inventory_cost_usd(storage, Symbol.from_string("BTC/USD"))
        assert inv == Decimal("22.09")

    @pytest.mark.asyncio
    async def test_sells_release_headroom_at_average(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(price="50000", amount="0.0004", minutes_ago=2))  # $20
        await storage.save_trade(
            _trade(side=OrderSide.SELL, price="55000", amount="0.0002", minutes_ago=1)
        )
        # Half the quantity sold -> half the cost basis remains.
        inv = await coin_inventory_cost_usd(storage, Symbol.from_string("BTC/USD"))
        assert inv == Decimal("10")

    @pytest.mark.asyncio
    async def test_no_trades_is_zero(self, storage: SQLiteStorageAdapter) -> None:
        assert await coin_inventory_cost_usd(storage, Symbol.from_string("BTC/USD")) == Decimal("0")

    @pytest.mark.asyncio
    async def test_total_spans_symbols(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(symbol="BTC/USD", price="50000", amount="0.0002"))
        await storage.save_trade(_trade(symbol="ETH/USD", price="2000", amount="0.005"))
        assert await total_inventory_cost_usd(storage) == Decimal("20")

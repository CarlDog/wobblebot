"""Tests for services.cost_basis.SellGuard (ADR-032)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.cost_basis import SellGuard

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_SYMBOL = Symbol(base="BTC", quote="USD")
_MAKER_FEE_RATE = Decimal("0.0026")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _trade(*, side: OrderSide, price: str, amount: str, minutes_offset: int = 0) -> Trade:
    px = Decimal(price)
    qty = Decimal(amount)
    return Trade(
        id=f"T-{side.value}-{minutes_offset}",
        order_id=f"O-{side.value}-{minutes_offset}",
        symbol=_SYMBOL,
        side=side,
        price=Price(amount=px, currency=_SYMBOL.quote),
        amount=Amount(value=qty, asset=_SYMBOL.base),
        fee=Decimal("0"),
        cost=px * qty,
        executed_at=Timestamp(
            dt=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes_offset)
        ),
    )


class TestSellGuardAssess:
    async def test_no_trade_history_allows_unguarded(self, storage: SQLiteStorageAdapter) -> None:
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        result = await guard.assess(_SYMBOL, Decimal("50"))
        assert result.allowed is True
        assert result.reason == "unknown_basis"

    async def test_sell_below_basis_beyond_tolerance_deferred(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.save_trade(_trade(side=OrderSide.BUY, price="100", amount="1"))
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        result = await guard.assess(_SYMBOL, Decimal("90"))
        assert result.allowed is False
        assert result.reason == "below_cost_basis"

    async def test_sell_above_basis_allowed(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(side=OrderSide.BUY, price="100", amount="1"))
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        result = await guard.assess(_SYMBOL, Decimal("110"))
        assert result.allowed is True


class TestSellGuardCache:
    async def test_basis_is_cached_across_assess_calls(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(side=OrderSide.BUY, price="100", amount="1"))
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        await guard.assess(_SYMBOL, Decimal("110"))

        # A trade saved directly to storage after the first assess() must
        # NOT be picked up without an explicit invalidate() -- proves the
        # cache is real, not accidentally bypassed.
        await storage.save_trade(
            _trade(side=OrderSide.BUY, price="1000", amount="1", minutes_offset=1)
        )
        second = await guard.assess(_SYMBOL, Decimal("110"))
        assert second.average_cost == Decimal("100")

    async def test_invalidate_forces_recompute(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(_trade(side=OrderSide.BUY, price="100", amount="1"))
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        await guard.assess(_SYMBOL, Decimal("110"))

        await storage.save_trade(
            _trade(side=OrderSide.BUY, price="1000", amount="1", minutes_offset=1)
        )
        guard.invalidate(_SYMBOL)
        second = await guard.assess(_SYMBOL, Decimal("110"))
        assert second.average_cost == Decimal("550")  # avg of 100 and 1000 over 2 BTC

    async def test_invalidate_on_unseen_symbol_is_a_no_op(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        guard = SellGuard(
            storage, max_loss_percentage=Decimal("1.0"), maker_fee_rate=_MAKER_FEE_RATE
        )
        guard.invalidate(_SYMBOL)  # must not raise

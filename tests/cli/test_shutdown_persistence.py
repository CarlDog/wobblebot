"""Tests for the Stage 8.1.B persistence-on-cancel fix.

Verifies the cli/live + cli/shadow shutdown loops persist the
``status="canceled"`` transition back to storage after each
successful ``adapter.cancel_order()`` call. Bug surfaced
2026-05-18 in a 60-minute shadow session: 3 BUYs cancelled per
log, all 3 still ``status="open"`` in shadow.db at exit.

The test exercises the ``_cancel_all_open`` helper directly with
a mock adapter + in-memory storage, asserting the storage row
transitions iff the adapter cancel succeeded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _cancel_all_open as _cancel_all_open_live
from wobblebot.cli.shadow import _cancel_all_open as _cancel_all_open_shadow
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_order(*, exchange_id: str = "OID-1", side: str = "buy") -> Order:
    return Order(
        id=uuid4(),
        exchange_id=exchange_id,
        symbol=Symbol(base="BTC", quote="USD"),
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal("30000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


# --------------------------------------------------------------------- #
# Mock adapter — drop-in for KrakenAdapter / ShadowExchangeAdapter in   #
# the shutdown loop. Records cancel calls and returns the orders in    #
# get_open_orders.                                                      #
# --------------------------------------------------------------------- #


class _FakeAdapter:
    """Just enough surface for ``_cancel_all_open``.

    Two knobs:
    - ``open_orders``: list returned by get_open_orders.
    - ``fail_on_cancel``: when True, cancel_order raises.

    Tracks every cancel call in ``cancelled_ids``.
    """

    def __init__(self, *, open_orders: list[Order], fail_on_cancel: bool = False) -> None:
        self._open = open_orders
        self._fail = fail_on_cancel
        self.cancelled_ids: list[str] = []

    async def get_open_orders(self, *, symbol: Symbol | None = None) -> list[Order]:
        if symbol is None:
            return list(self._open)
        return [
            o for o in self._open if o.symbol.base == symbol.base and o.symbol.quote == symbol.quote
        ]

    async def cancel_order(self, order: Order) -> None:
        if self._fail:
            raise ExchangeError("simulated cancel failure")
        self.cancelled_ids.append(order.exchange_id or "")


# --------------------------------------------------------------------- #
# cli/live persistence                                                  #
# --------------------------------------------------------------------- #


class TestLivePersistenceOnCancel:
    async def test_storage_row_transitions_to_canceled(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order(exchange_id="OID-A")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        cancelled, failed = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 1
        assert failed == 0
        assert adapter.cancelled_ids == ["OID-A"]
        # The storage row's status now reflects the cancellation.
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"

    async def test_cancel_failure_leaves_storage_at_open(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Don't lie in the audit trail: if the cancel raised, the
        storage row stays ``status='open'`` so reconciliation knows
        the order may still exist."""
        order = _make_order(exchange_id="OID-B")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order], fail_on_cancel=True)

        cancelled, failed = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 0
        assert failed == 1
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "open"

    async def test_multi_order_all_persist(self, storage: SQLiteStorageAdapter) -> None:
        orders = [_make_order(exchange_id=f"OID-{i}") for i in range(3)]
        for o in orders:
            await storage.save_order(o)
        adapter = _FakeAdapter(open_orders=orders)

        cancelled, _ = await _cancel_all_open_live(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 3
        for o in orders:
            roundtripped = await storage.get_order(o.id)
            assert roundtripped is not None
            assert roundtripped.status == "canceled"


# --------------------------------------------------------------------- #
# cli/shadow persistence                                                #
# --------------------------------------------------------------------- #


class TestShadowPersistenceOnCancel:
    async def test_storage_row_transitions_to_canceled(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order(exchange_id="SHDW-A")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        cancelled, failed = await _cancel_all_open_shadow(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 1
        assert failed == 0
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"

    async def test_cancel_failure_leaves_storage_at_open(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _make_order(exchange_id="SHDW-B")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order], fail_on_cancel=True)

        cancelled, failed = await _cancel_all_open_shadow(
            adapter,  # type: ignore[arg-type]
            storage,
            (Symbol(base="BTC", quote="USD"),),
        )
        assert cancelled == 0
        assert failed == 1
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "open"

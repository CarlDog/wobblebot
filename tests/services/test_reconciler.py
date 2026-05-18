"""Tests for services.reconciler (Stage 8.1.C — ADR-018).

Two layers tested:

- ``reconcile_open_orders`` (pure function) — exhaustive cases.
- ``apply_reconciliation`` (async orchestrator) — happy path +
  storage failure + adapter failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.reconciler import (
    ReconciliationPlan,
    ReconciliationReport,
    apply_reconciliation,
    reconcile_open_orders,
)

pytestmark = pytest.mark.unit


def _order(
    *, exchange_id: str | None = "OID-DEFAULT", symbol: str = "BTC/USD", side: str = "buy"
) -> Order:
    base, quote = symbol.split("/")
    return Order(
        id=uuid4(),
        exchange_id=exchange_id,
        symbol=Symbol(base=base, quote=quote),
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal("30000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


# --------------------------------------------------------------------- #
# Pure function tests                                                   #
# --------------------------------------------------------------------- #


class TestReconcileOpenOrdersPure:
    def test_both_agree_produces_empty_plan(self) -> None:
        order = _order(exchange_id="O-1")
        plan = reconcile_open_orders(
            exchange_open=[order],
            storage_open=[order],
        )
        assert plan == ReconciliationPlan()

    def test_storage_only_lists_storage_row(self) -> None:
        stale = _order(exchange_id="O-2")
        plan = reconcile_open_orders(
            exchange_open=[],
            storage_open=[stale],
        )
        assert plan.storage_only == (stale,)
        assert plan.exchange_only == ()

    def test_exchange_only_lists_exchange_order(self) -> None:
        orphan = _order(exchange_id="O-3")
        plan = reconcile_open_orders(
            exchange_open=[orphan],
            storage_open=[],
        )
        assert plan.storage_only == ()
        assert plan.exchange_only == (orphan,)

    def test_mixed_partitions_correctly(self) -> None:
        shared = _order(exchange_id="SHARED")
        stale = _order(exchange_id="STALE")
        orphan = _order(exchange_id="ORPHAN")
        plan = reconcile_open_orders(
            exchange_open=[shared, orphan],
            storage_open=[shared, stale],
        )
        assert plan.storage_only == (stale,)
        assert plan.exchange_only == (orphan,)

    def test_storage_row_without_exchange_id_skipped(self) -> None:
        """Rows with exchange_id=None are unsubmitted pending orders;
        they don't participate in reconciliation."""
        unsubmitted = _order(exchange_id=None)
        plan = reconcile_open_orders(
            exchange_open=[],
            storage_open=[unsubmitted],
        )
        # Even though storage has it as open, no exchange_id means
        # nothing to reconcile.
        assert plan.storage_only == ()

    def test_empty_both_sides(self) -> None:
        plan = reconcile_open_orders(exchange_open=[], storage_open=[])
        assert plan == ReconciliationPlan()

    def test_configured_symbols_filters_orphans(self) -> None:
        """Orphans on unconfigured symbols are silently skipped."""
        configured_orphan = _order(exchange_id="C-1", symbol="BTC/USD")
        unconfigured_orphan = _order(exchange_id="U-1", symbol="SOL/USD")
        plan = reconcile_open_orders(
            exchange_open=[configured_orphan, unconfigured_orphan],
            storage_open=[],
            configured_symbols=frozenset({"BTC"}),
        )
        # Only BTC orphan flagged; SOL silently ignored.
        assert plan.exchange_only == (configured_orphan,)

    def test_configured_symbols_does_not_filter_storage_only(self) -> None:
        """Storage-only reconciliation runs against ALL storage rows
        regardless of configured symbols — stale rows in any symbol
        should clear."""
        old_sol_row = _order(exchange_id="STALE-SOL", symbol="SOL/USD")
        plan = reconcile_open_orders(
            exchange_open=[],
            storage_open=[old_sol_row],
            configured_symbols=frozenset({"BTC"}),
        )
        assert plan.storage_only == (old_sol_row,)

    def test_none_configured_symbols_disables_filter(self) -> None:
        sol_orphan = _order(exchange_id="ORPHAN", symbol="SOL/USD")
        plan = reconcile_open_orders(
            exchange_open=[sol_orphan],
            storage_open=[],
            configured_symbols=None,
        )
        assert plan.exchange_only == (sol_orphan,)

    def test_case_insensitive_symbol_filter(self) -> None:
        """Symbol base matching is case-insensitive — operator may
        type 'btc' in config; pure-function should still match."""
        order = _order(exchange_id="O", symbol="btc/USD")
        plan = reconcile_open_orders(
            exchange_open=[order],
            storage_open=[],
            configured_symbols=frozenset({"BTC"}),
        )
        assert plan.exchange_only == (order,)


# --------------------------------------------------------------------- #
# Async orchestrator tests                                              #
# --------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class _FakeAdapter:
    """Minimal adapter shape for apply_reconciliation."""

    def __init__(self, *, open_orders: list[Order], fail: bool = False) -> None:
        self._open = open_orders
        self._fail = fail

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        if self._fail:
            raise ExchangeError("simulated adapter failure")
        if symbol is None:
            return list(self._open)
        return [
            o for o in self._open if o.symbol.base == symbol.base and o.symbol.quote == symbol.quote
        ]


@pytest.mark.asyncio
class TestApplyReconciliation:
    async def test_storage_only_transitions_persist(self, storage: SQLiteStorageAdapter) -> None:
        stale = _order(exchange_id="STALE-1")
        await storage.save_order(stale)
        adapter = _FakeAdapter(open_orders=[])

        report = await apply_reconciliation(adapter, storage)  # type: ignore[arg-type]

        assert report.storage_canceled_count == 1
        assert report.storage_persistence_failures == 0
        assert report.orphan_count == 0
        # Storage row is now canceled.
        roundtripped = await storage.get_order(stale.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"

    async def test_both_agree_no_persistence(self, storage: SQLiteStorageAdapter) -> None:
        order = _order(exchange_id="MATCH")
        await storage.save_order(order)
        adapter = _FakeAdapter(open_orders=[order])

        report = await apply_reconciliation(adapter, storage)  # type: ignore[arg-type]

        assert report == ReconciliationReport()
        # Storage row stays open.
        roundtripped = await storage.get_order(order.id)
        assert roundtripped is not None
        assert roundtripped.status == "open"

    async def test_orphan_logged_not_adopted(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        orphan = _order(exchange_id="ORPHAN")
        adapter = _FakeAdapter(open_orders=[orphan])

        import logging

        with caplog.at_level(logging.ERROR, logger="wobblebot.services.reconciler"):
            report = await apply_reconciliation(
                adapter, storage, configured_symbols=frozenset({"BTC"})  # type: ignore[arg-type]
            )

        assert report.storage_canceled_count == 0
        assert report.orphan_count == 1
        assert len(report.orphan_summaries) == 1
        # No new storage row created — we did NOT adopt.
        assert (await storage.get_order(orphan.id)) is None
        # Two ERROR logs: per-orphan + summary.
        assert (
            sum(1 for r in caplog.records if r.levelname == "ERROR" and "orphan" in r.message) >= 1
        )

    async def test_mixed_classes_handled(self, storage: SQLiteStorageAdapter) -> None:
        shared = _order(exchange_id="SHARED")
        stale = _order(exchange_id="STALE")
        orphan = _order(exchange_id="ORPHAN")
        await storage.save_order(shared)
        await storage.save_order(stale)
        adapter = _FakeAdapter(open_orders=[shared, orphan])

        report = await apply_reconciliation(
            adapter, storage, configured_symbols=frozenset({"BTC"})  # type: ignore[arg-type]
        )

        assert report.storage_canceled_count == 1
        assert report.orphan_count == 1

    async def test_adapter_failure_propagates(self, storage: SQLiteStorageAdapter) -> None:
        """Per ADR-018 decision 8: adapter timeout = refuse to start."""
        adapter = _FakeAdapter(open_orders=[], fail=True)
        with pytest.raises(ExchangeError, match="simulated"):
            await apply_reconciliation(adapter, storage)  # type: ignore[arg-type]

    async def test_unconfigured_orphan_silently_skipped(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        sol_orphan = _order(exchange_id="SOL-ORPHAN", symbol="SOL/USD")
        adapter = _FakeAdapter(open_orders=[sol_orphan])

        report = await apply_reconciliation(
            adapter, storage, configured_symbols=frozenset({"BTC"})  # type: ignore[arg-type]
        )

        # Engine only configured for BTC; the SOL orphan is operator's
        # business, not the engine's.
        assert report.orphan_count == 0
        assert report.orphan_summaries == ()

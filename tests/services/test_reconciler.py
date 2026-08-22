"""Tests for services.reconciler (Stage 8.1.C — ADR-018).

Two layers tested:

- ``reconcile_open_orders`` (pure function) — exhaustive cases.
- ``apply_reconciliation`` (async orchestrator) — happy path +
  storage failure + adapter failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError, StorageError
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
    """Minimal adapter shape for apply_reconciliation.

    ``terminal_orders`` maps ``exchange_id`` -> the order
    ``get_order_status`` should return (ADR-023). Absent from the map
    defaults to a genuine clean cancel (``filled_amount=0``) so
    existing storage-only tests need no changes. ``trades`` feeds
    ``get_trade_history`` for the recovered-fill path.
    """

    def __init__(
        self,
        *,
        open_orders: list[Order],
        fail: bool = False,
        terminal_orders: dict[str, Order] | None = None,
        trades: list[Trade] | None = None,
    ) -> None:
        self._open = open_orders
        self._fail = fail
        self._terminal_orders = terminal_orders or {}
        self._trades = trades or []

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        if self._fail:
            raise ExchangeError("simulated adapter failure")
        if symbol is None:
            return list(self._open)
        return [
            o for o in self._open if o.symbol.base == symbol.base and o.symbol.quote == symbol.quote
        ]

    async def get_order_status(self, order: Order) -> Order:
        if order.exchange_id and order.exchange_id in self._terminal_orders:
            return self._terminal_orders[order.exchange_id]
        return order.model_copy(update={"status": "canceled", "filled_amount": Decimal("0")})

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        if symbol is None:
            return list(self._trades)
        return [t for t in self._trades if t.symbol == symbol][:limit]


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


# --------------------------------------------------------------------- #
# ADR-023: recovered fills on storage-only orders                       #
# --------------------------------------------------------------------- #


def _trade_for(order: Order, *, filled_amount: str) -> Trade:
    return Trade(
        id=f"T-{order.exchange_id}",
        order_id=order.exchange_id or "",
        symbol=order.symbol,
        side=order.side,
        price=order.price,
        amount=Amount(value=Decimal(filled_amount), asset=order.symbol.base),
        fee=Decimal("0"),
        cost=order.price.amount * Decimal(filled_amount),
        executed_at=Timestamp(dt=datetime.now(UTC)),
    )


@pytest.mark.asyncio
class TestApplyReconciliationRecoveredFills:
    async def test_full_fill_recovered_not_marked_canceled(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The 2026-05-19 orphan class: a BUY fully filled while the
        daemon was down. Old behavior would mark it canceled and drop
        the trade; ADR-023 recovers it instead."""
        stale = _order(exchange_id="FILLED-1")
        await storage.save_order(stale)
        closed = stale.model_copy(update={"status": "closed", "filled_amount": stale.amount.value})
        trade = _trade_for(closed, filled_amount=str(stale.amount.value))
        adapter = _FakeAdapter(open_orders=[], terminal_orders={"FILLED-1": closed}, trades=[trade])

        report = await apply_reconciliation(adapter, storage)

        assert report.storage_canceled_count == 0
        assert report.recovered_fill_count == 1
        assert report.needs_counter_order_ids == (stale.id,)
        roundtripped = await storage.get_order(stale.id)
        assert roundtripped is not None
        assert roundtripped.status == "closed"
        assert roundtripped.filled_amount == stale.amount.value
        trades = await storage.get_trades(symbol=stale.symbol)
        assert len(trades) == 1

    async def test_partial_cancel_recovered_f1_case(self, storage: SQLiteStorageAdapter) -> None:
        """F1: an order that partially filled before a cancel/expiry.
        Kraken reports status=canceled WITH filled_amount > 0 -- the
        state the old code silently dropped."""
        stale = _order(exchange_id="PARTIAL-1")
        await storage.save_order(stale)
        partial_qty = stale.amount.value / 2
        canceled_with_fill = stale.model_copy(
            update={"status": "canceled", "filled_amount": partial_qty}
        )
        trade = _trade_for(canceled_with_fill, filled_amount=str(partial_qty))
        adapter = _FakeAdapter(
            open_orders=[],
            terminal_orders={"PARTIAL-1": canceled_with_fill},
            trades=[trade],
        )

        report = await apply_reconciliation(adapter, storage)

        assert report.recovered_fill_count == 1
        assert report.storage_canceled_count == 0
        assert report.needs_counter_order_ids == (stale.id,)
        roundtripped = await storage.get_order(stale.id)
        assert roundtripped is not None
        assert roundtripped.status == "canceled"
        assert roundtripped.filled_amount == partial_qty
        trades = await storage.get_trades(symbol=stale.symbol)
        assert len(trades) == 1
        assert trades[0].amount.value == partial_qty

    async def test_clean_cancel_still_marked_canceled_no_counter(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        stale = _order(exchange_id="CLEAN-1")
        await storage.save_order(stale)
        adapter = _FakeAdapter(open_orders=[])  # default: filled_amount=0

        report = await apply_reconciliation(adapter, storage)

        assert report.storage_canceled_count == 1
        assert report.recovered_fill_count == 0
        assert report.needs_counter_order_ids == ()

    async def test_storage_persistence_failure_on_recovered_fill_is_counted(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A storage failure while persisting a recovered fill must be
        logged + counted, not silently swallowed or raised -- one bad
        row doesn't block boot."""
        stale = _order(exchange_id="FAIL-SAVE-1")
        await storage.save_order(stale)
        closed = stale.model_copy(update={"status": "closed", "filled_amount": stale.amount.value})
        trade = _trade_for(closed, filled_amount=str(stale.amount.value))
        adapter = _FakeAdapter(
            open_orders=[], terminal_orders={"FAIL-SAVE-1": closed}, trades=[trade]
        )

        class _FailingSaveStorage(SQLiteStorageAdapter):
            async def save_fill(  # type: ignore[override]
                self, order: Order, trades: Sequence[Trade]
            ) -> None:
                raise StorageError("simulated save failure")

        failing_storage = _FailingSaveStorage(":memory:")
        # Reuse the same underlying connection so get_open_orders still
        # sees the row `storage` just saved.
        failing_storage._conn = storage._conn  # type: ignore[attr-defined] # pylint: disable=protected-access

        report = await apply_reconciliation(adapter, failing_storage)

        assert report.storage_persistence_failures == 1
        assert report.recovered_fill_count == 0

    async def test_one_bad_row_does_not_block_reconciliation_of_the_rest(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """v1.1 test-hardening (test-honesty audit, P9 "reconciler
        fail-soft"): the prior failure test above uses a single
        storage-only order, so it proves the exception doesn't
        propagate but NOT that the loop actually continues on to the
        next row. Here two storage-only orders are in play and only
        the first's persistence fails -- a regression that turned the
        per-row ``except StorageError`` into a ``re-raise`` (aborting
        the whole reconciliation, and daemon boot with it) would still
        pass every other reconciler test but fail this one: the second
        (good) row must still get its clean-cancel persisted."""
        bad = _order(exchange_id="BAD-1")
        good = _order(exchange_id="GOOD-1")
        await storage.save_order(bad)
        await storage.save_order(good)
        adapter = _FakeAdapter(open_orders=[])  # both resolve to a clean cancel

        class _SelectivelyFailingStorage(SQLiteStorageAdapter):
            async def save_order(self, order: Order) -> None:  # type: ignore[override]
                if order.exchange_id == "BAD-1":
                    raise StorageError("simulated save failure")
                await super().save_order(order)

        failing_storage = _SelectivelyFailingStorage(":memory:")
        # Reuse the same underlying connection so get_open_orders still
        # sees the rows `storage` just saved.
        failing_storage._conn = storage._conn  # type: ignore[attr-defined] # pylint: disable=protected-access

        report = await apply_reconciliation(adapter, failing_storage)

        assert report.storage_persistence_failures == 1
        assert report.storage_canceled_count == 1
        bad_roundtripped = await storage.get_order(bad.id)
        assert bad_roundtripped is not None
        assert bad_roundtripped.status == "open", "the failed row stays open, untouched"
        good_roundtripped = await storage.get_order(good.id)
        assert good_roundtripped is not None
        assert (
            good_roundtripped.status == "canceled"
        ), "reconciliation must continue past the bad row"

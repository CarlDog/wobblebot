"""ADR-046 storage contract: a confirmed fill without its trade rows is
pending, never final.

Two halves. The ``save_fill`` guard refuses the exact 2026-09-10 shape
(``filled_amount > 0``, empty trades) before touching the database, so
no future call path can quietly recreate the loss. The
``pending_fill_trades`` marker methods persist the order's terminal
state together with an "owed trades" row in one transaction, record
recovered rows, count empty lookups, and mark abandonment without
deleting the forensic row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import aiosqlite
import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DOGE_USD = Symbol(base="DOGE", quote="USD")
TXID = "OLG4OV-BXTHW-T6IS2H"


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _open_order(exchange_id: str | None = TXID) -> Order:
    order = Order(
        symbol=DOGE_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("0.083118654"), currency="USD"),
        amount=Amount(value=Decimal("72.18596201"), asset="DOGE"),
        created_at=Timestamp(dt=datetime(2026, 9, 9, 1, 23, tzinfo=UTC)),
    )
    if exchange_id:
        order.mark_open(exchange_id)
    return order


def _filled(order: Order, amount: str = "72.18596201") -> Order:
    return order.model_copy(update={"status": "closed", "filled_amount": Decimal(amount)})


def _trade(trade_id: str, amount: str, order_id: str = TXID) -> Trade:
    qty = Decimal(amount)
    return Trade(
        id=trade_id,
        order_id=order_id,
        symbol=DOGE_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("0.0831186"), currency="USD"),
        amount=Amount(value=qty, asset="DOGE"),
        fee=Decimal("0.024"),
        cost=Decimal("0.0831186") * qty,
        executed_at=Timestamp(dt=datetime(2026, 9, 10, 12, 47, 13, tzinfo=UTC)),
    )


class TestSaveFillGuard:
    async def test_refuses_filled_order_with_no_trades_and_writes_nothing(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)

        with pytest.raises(StorageError, match="pending, not final"):
            await storage.save_fill(_filled(order), [])

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "open"
        assert loaded.filled_amount == 0
        assert await storage.get_trades(symbol=DOGE_USD) == []

    async def test_still_accepts_a_clean_cancel_with_no_trades(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)

        await storage.save_fill(order.model_copy(update={"status": "canceled"}), [])

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "canceled"

    async def test_still_accepts_a_fill_with_its_trades(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)

        await storage.save_fill(_filled(order), [_trade("TEGXTG-FHHBB-375Q4L", "72.18596201")])

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "closed"
        assert len(await storage.get_trades(symbol=DOGE_USD)) == 1


class TestSaveFillPendingTrades:
    async def test_closes_order_and_writes_marker_in_one_go(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)

        await storage.save_fill_pending_trades(_filled(order))

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "closed"
        assert loaded.filled_amount == Decimal("72.18596201")
        pending = await storage.get_pending_fill_trades()
        assert len(pending) == 1
        marker = pending[0]
        assert marker.order_id == order.id
        assert marker.exchange_id == TXID
        assert marker.symbol == DOGE_USD
        assert marker.filled_amount == Decimal("72.18596201")
        assert marker.attempts == 0
        assert marker.last_attempt_at is None
        assert marker.given_up_at is None
        assert await storage.get_trades(symbol=DOGE_USD) == []

    async def test_partial_trades_are_saved_alongside_the_marker(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)

        await storage.save_fill_pending_trades(_filled(order), [_trade("T-PART-1", "40")])

        assert [t.id for t in await storage.get_trades(symbol=DOGE_USD)] == ["T-PART-1"]
        assert len(await storage.get_pending_fill_trades(DOGE_USD)) == 1

    async def test_refuses_zero_fill(self, storage: SQLiteStorageAdapter) -> None:
        order = _open_order()
        await storage.save_order(order)

        with pytest.raises(StorageError, match="not a fill"):
            await storage.save_fill_pending_trades(order.model_copy(update={"status": "canceled"}))
        assert await storage.get_pending_fill_trades() == []

    async def test_refuses_missing_exchange_id(self, storage: SQLiteStorageAdapter) -> None:
        order = _open_order(exchange_id=None)
        with pytest.raises(StorageError, match="no exchange_id"):
            await storage.save_fill_pending_trades(_filled(order))

    async def test_marker_write_failure_rolls_back_the_order_close(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The order and the marker must never be separated: if the
        marker insert fails the order stays open so the next tick
        re-resolves it, exactly like save_fill's own guarantee."""
        order = _open_order()
        await storage.save_order(order)

        class _FailOnMarker(SQLiteStorageAdapter):
            async def _execute_save_trade(  # type: ignore[override]
                self, conn: aiosqlite.Connection, trade: Trade
            ) -> None:
                raise aiosqlite.OperationalError("simulated failure inside the transaction")

        failing = _FailOnMarker(":memory:")
        failing._conn = storage._conn  # pylint: disable=protected-access

        with pytest.raises(StorageError):
            await failing.save_fill_pending_trades(_filled(order), [_trade("T-X", "1")])

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "open"
        assert await storage.get_pending_fill_trades() == []
        assert await storage.get_trades(symbol=DOGE_USD) == []

    async def test_resaving_same_order_keeps_first_seen_and_attempts(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)
        await storage.save_fill_pending_trades(_filled(order))
        await storage.note_pending_fill_trades_attempt(
            order.id, at=Timestamp(dt=datetime(2026, 9, 10, 12, 47, 25, tzinfo=UTC)), given_up=False
        )
        first = (await storage.get_pending_fill_trades())[0]

        await storage.save_fill_pending_trades(_filled(order))

        again = (await storage.get_pending_fill_trades())[0]
        assert again.first_seen_at == first.first_seen_at
        assert again.attempts == 1


class TestRecordAndNote:
    async def _pending(self, storage: SQLiteStorageAdapter) -> Order:
        order = _open_order()
        await storage.save_order(order)
        await storage.save_fill_pending_trades(_filled(order))
        return order

    async def test_record_incomplete_keeps_marker(self, storage: SQLiteStorageAdapter) -> None:
        order = await self._pending(storage)

        await storage.record_pending_fill_trades(order.id, [_trade("T-1", "30")], complete=False)

        assert [t.id for t in await storage.get_trades(symbol=DOGE_USD)] == ["T-1"]
        assert len(await storage.get_pending_fill_trades()) == 1

    async def test_record_complete_deletes_marker_and_is_idempotent(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = await self._pending(storage)
        trades = [_trade("T-1", "30"), _trade("T-2", "42.18596201")]

        await storage.record_pending_fill_trades(order.id, trades, complete=True)
        await storage.record_pending_fill_trades(order.id, trades, complete=True)

        saved = await storage.get_trades(symbol=DOGE_USD)
        assert sorted(t.id for t in saved) == ["T-1", "T-2"]
        assert await storage.get_pending_fill_trades(include_given_up=True) == []

    async def test_note_attempt_counts_and_given_up_hides_by_default(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = await self._pending(storage)
        t1 = Timestamp(dt=datetime(2026, 9, 10, 12, 47, 25, tzinfo=UTC))
        t2 = Timestamp(dt=datetime(2026, 9, 10, 12, 47, 31, tzinfo=UTC))

        await storage.note_pending_fill_trades_attempt(order.id, at=t1, given_up=False)
        marker = (await storage.get_pending_fill_trades())[0]
        assert marker.attempts == 1
        assert marker.last_attempt_at == t1
        assert marker.given_up_at is None

        await storage.note_pending_fill_trades_attempt(order.id, at=t2, given_up=True)
        assert await storage.get_pending_fill_trades() == []
        kept = await storage.get_pending_fill_trades(include_given_up=True)
        assert len(kept) == 1
        assert kept[0].attempts == 2
        assert kept[0].given_up_at == t2

    async def test_symbol_filter_and_oldest_first(self, storage: SQLiteStorageAdapter) -> None:
        doge = _open_order()
        await storage.save_order(doge)
        await storage.save_fill_pending_trades(_filled(doge))
        sol = Order(
            symbol=Symbol(base="SOL", quote="USD"),
            side=OrderSide.SELL,
            price=Price(amount=Decimal("112.5"), currency="USD"),
            amount=Amount(value=Decimal("0.07"), asset="SOL"),
            created_at=Timestamp(dt=datetime(2026, 9, 18, tzinfo=UTC)),
        )
        sol.mark_open("OSOL00-000000-000001")
        await storage.save_order(sol)
        await storage.save_fill_pending_trades(_filled(sol, "0.07"))

        only_doge = await storage.get_pending_fill_trades(DOGE_USD)
        assert [m.exchange_id for m in only_doge] == [TXID]
        both = await storage.get_pending_fill_trades()
        assert [m.exchange_id for m in both] == [TXID, "OSOL00-000000-000001"]


class TestReviewRoundPins:
    async def test_marker_insert_failure_alone_rolls_back_the_order_close(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The 2026-09-18 review found the earlier rollback test failed the
        TRADE insert, so a commit slipped in before the marker write would
        pass it. This fails the marker statement itself."""
        order = _open_order()
        await storage.save_order(order)

        class _FailOnMarkerOnly(SQLiteStorageAdapter):
            async def _execute_save_pending_marker(  # type: ignore[override]
                self, conn: aiosqlite.Connection, order: Order, first_seen_iso: str
            ) -> None:
                raise aiosqlite.OperationalError("simulated failure on the marker insert")

        failing = _FailOnMarkerOnly(":memory:")
        failing._conn = storage._conn  # pylint: disable=protected-access

        with pytest.raises(StorageError):
            await failing.save_fill_pending_trades(_filled(order), [_trade("T-1", "30")])

        loaded = await storage.get_order(order.id)
        assert loaded is not None and loaded.status == "open"
        assert await storage.get_trades(symbol=DOGE_USD) == []
        assert await storage.get_pending_fill_trades(include_given_up=True) == []

    async def test_uncounted_attempt_stamps_time_but_not_the_count(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        order = _open_order()
        await storage.save_order(order)
        await storage.save_fill_pending_trades(_filled(order))
        t1 = Timestamp(dt=datetime(2026, 9, 10, 12, 47, 25, tzinfo=UTC))

        await storage.note_pending_fill_trades_attempt(
            order.id, at=t1, given_up=False, counted=False
        )

        marker = (await storage.get_pending_fill_trades())[0]
        assert marker.attempts == 0
        assert marker.last_attempt_at == t1

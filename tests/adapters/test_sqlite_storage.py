"""Unit tests for SQLiteStorageAdapter.

Tests run against an in-memory SQLite database (`:memory:`) to keep them
fast and isolated. Each test gets a fresh adapter via the `storage` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_order(
    *,
    symbol: Symbol | None = None,
    side: str = "buy",
    price_amount: str = "50000",
    amount_value: str = "0.1",
    status: str = "pending",
) -> Order:
    return Order(
        symbol=symbol or Symbol(base="BTC", quote="USD"),
        side=OrderSide(side),
        price=Price(amount=Decimal(price_amount), currency="USD"),
        amount=Amount(value=Decimal(amount_value), asset="BTC"),
        status=status,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _make_trade(
    *,
    trade_id: str = "TRADE-001",
    order_id: str = "ORDER-001",
    executed_at: datetime | None = None,
    symbol: Symbol | None = None,
) -> Trade:
    return Trade(
        id=trade_id,
        order_id=order_id,
        symbol=symbol or Symbol(base="BTC", quote="USD"),
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000.12345678"), currency="USD"),
        amount=Amount(value=Decimal("0.05"), asset="BTC"),
        fee=Decimal("0.25"),
        cost=Decimal("2500.0617"),
        executed_at=Timestamp(dt=executed_at or datetime.now(UTC)),
    )


class TestGetOrders:
    """Tests for the filtered ``get_orders`` (Stage 2.2.4 — used by safety caps)."""

    async def test_no_filters_returns_all(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_order(_make_order(side="buy"))
        await storage.save_order(_make_order(side="sell"))
        results = await storage.get_orders()
        assert len(results) == 2

    async def test_symbol_filter(self, storage: SQLiteStorageAdapter) -> None:
        btc = Symbol(base="BTC", quote="USD")
        eth = Symbol(base="ETH", quote="USD")
        await storage.save_order(_make_order(symbol=btc))
        await storage.save_order(_make_order(symbol=eth))
        btc_only = await storage.get_orders(symbol=btc)
        assert len(btc_only) == 1
        assert btc_only[0].symbol == btc

    async def test_side_filter(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_order(_make_order(side="buy"))
        await storage.save_order(_make_order(side="sell"))
        await storage.save_order(_make_order(side="buy"))
        buys = await storage.get_orders(side="buy")
        assert len(buys) == 2
        assert all(o.side is OrderSide.BUY for o in buys)

    async def test_created_after_filter_excludes_older(self, storage: SQLiteStorageAdapter) -> None:
        old_order = Order(
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide.BUY,
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            status="pending",
            created_at=Timestamp(dt=datetime.now(UTC) - timedelta(days=1)),
        )
        new_order = _make_order()
        await storage.save_order(old_order)
        await storage.save_order(new_order)
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        recent = await storage.get_orders(created_after=cutoff)
        assert len(recent) == 1
        assert recent[0].id == new_order.id

    async def test_combined_filters(self, storage: SQLiteStorageAdapter) -> None:
        # Set up: 2 BTC BUYs (one old, one new), 1 ETH BUY (new), 1 BTC SELL (new).
        btc = Symbol(base="BTC", quote="USD")
        eth = Symbol(base="ETH", quote="USD")
        old_btc_buy = Order(
            symbol=btc,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            status="canceled",
            created_at=Timestamp(dt=datetime.now(UTC) - timedelta(days=2)),
        )
        await storage.save_order(old_btc_buy)
        await storage.save_order(_make_order(symbol=btc, side="buy"))  # new BTC BUY
        await storage.save_order(_make_order(symbol=eth, side="buy"))  # new ETH BUY
        await storage.save_order(_make_order(symbol=btc, side="sell"))  # new BTC SELL

        cutoff = datetime.now(UTC) - timedelta(hours=1)
        results = await storage.get_orders(symbol=btc, side="buy", created_after=cutoff)
        # Only the new BTC BUY matches all three filters.
        assert len(results) == 1
        assert results[0].symbol == btc
        assert results[0].side is OrderSide.BUY


class TestConnectionLifecycle:
    async def test_operations_fail_before_connect(self) -> None:
        adapter = SQLiteStorageAdapter(":memory:")
        with pytest.raises(StorageError, match="not connected"):
            await adapter.get_order(uuid4())

    async def test_double_connect_is_idempotent(self) -> None:
        adapter = SQLiteStorageAdapter(":memory:")
        await adapter.connect()
        await adapter.connect()
        await adapter.close()

    async def test_close_without_connect_is_noop(self) -> None:
        adapter = SQLiteStorageAdapter(":memory:")
        await adapter.close()

    async def test_connect_creates_missing_parent_dirs(self, tmp_path: Path) -> None:
        # Verifies the deprived-env fix: an operator who hasn't run
        # `mkdir data/` shouldn't get a raw sqlite3.OperationalError.
        nested = tmp_path / "missing_dir" / "deeper" / "wobblebot.db"
        assert not nested.parent.exists()
        adapter = SQLiteStorageAdapter(nested)
        try:
            await adapter.connect()
            assert nested.parent.exists()
            assert nested.exists()
        finally:
            await adapter.close()

    async def test_connect_with_in_memory_path_unchanged(self) -> None:
        # ":memory:" is a sentinel — it must not trigger filesystem writes.
        adapter = SQLiteStorageAdapter(":memory:")
        try:
            await adapter.connect()
        finally:
            await adapter.close()


class TestStage83Pragmas:
    """Stage 8.3.B — performance pragmas applied at connect().

    On-disk DBs get ``journal_mode=WAL`` + ``synchronous=NORMAL`` for
    concurrent-reader and commit-throughput wins; in-memory DBs skip
    those two (no fsync to drop, no WAL file to write). foreign_keys
    is enabled for every connection regardless.
    """

    async def test_on_disk_journal_mode_is_wal(self, tmp_path: Path) -> None:
        adapter = SQLiteStorageAdapter(tmp_path / "wal.db")
        try:
            await adapter.connect()
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute("PRAGMA journal_mode") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert str(row[0]).lower() == "wal"
        finally:
            await adapter.close()

    async def test_on_disk_synchronous_is_normal(self, tmp_path: Path) -> None:
        adapter = SQLiteStorageAdapter(tmp_path / "sync.db")
        try:
            await adapter.connect()
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute("PRAGMA synchronous") as cur:
                row = await cur.fetchone()
            # SQLite reports synchronous as integer: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
            assert row is not None
            assert int(row[0]) == 1

        finally:
            await adapter.close()

    async def test_on_disk_foreign_keys_on(self, tmp_path: Path) -> None:
        adapter = SQLiteStorageAdapter(tmp_path / "fk.db")
        try:
            await adapter.connect()
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute("PRAGMA foreign_keys") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert int(row[0]) == 1
        finally:
            await adapter.close()

    async def test_in_memory_journal_mode_not_wal(self) -> None:
        # WAL is a no-op for in-memory DBs; the design doc decision 4
        # says skip the pragma so fixtures that introspect journal_mode
        # see the engine default ("memory") rather than "wal".
        adapter = SQLiteStorageAdapter(":memory:")
        try:
            await adapter.connect()
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute("PRAGMA journal_mode") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert str(row[0]).lower() != "wal"
        finally:
            await adapter.close()

    async def test_in_memory_foreign_keys_still_on(self) -> None:
        # foreign_keys is applied regardless of in-memory vs on-disk —
        # only WAL + synchronous are conditional.
        adapter = SQLiteStorageAdapter(":memory:")
        try:
            await adapter.connect()
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute("PRAGMA foreign_keys") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert int(row[0]) == 1
        finally:
            await adapter.close()


class TestStage83IndexAudit:
    """Stage 8.3.C — every hot read uses an index, not a table scan.

    Runs ``EXPLAIN QUERY PLAN`` on the engine's per-tick reads
    (cli/live, cli/operator, cli/maintenance) and asserts the output
    contains ``SEARCH`` (index access) and never ``SCAN`` (full table
    scan). A regression here means a new query slipped past the
    schema's index coverage and will degrade as row count grows.

    These tests use the ``storage`` fixture's in-memory DB — the
    planner sees the same schema either way; the actual disk-vs-memory
    distinction is for pragma behavior, not query planning.
    """

    @staticmethod
    async def _eqp(storage: SQLiteStorageAdapter, sql: str, params: tuple) -> str:
        conn = storage._require_conn()  # pylint: disable=protected-access
        async with conn.execute(f"EXPLAIN QUERY PLAN {sql}", params) as cur:
            rows = await cur.fetchall()
        return "\n".join(str(r[3]) for r in rows)

    async def test_get_open_orders_by_symbol_uses_index(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        plan = await self._eqp(
            storage,
            "SELECT * FROM orders WHERE status IN ('pending', 'open') "
            "AND symbol_base = ? AND symbol_quote = ? ORDER BY created_at",
            ("BTC", "USD"),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan

    async def test_get_trades_by_symbol_and_time_uses_index(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        plan = await self._eqp(
            storage,
            "SELECT * FROM trades WHERE symbol_base=? AND symbol_quote=? "
            "AND executed_at >= ? ORDER BY executed_at DESC LIMIT ?",
            ("BTC", "USD", "2026-01-01T00:00:00+00:00", 100),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan

    async def test_pending_commands_by_status_uses_index(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # cli/live polls WHERE status='approved'; cli/operator's TTL
        # expirer polls WHERE status='awaiting_confirmation'.
        plan = await self._eqp(
            storage,
            "SELECT * FROM pending_commands WHERE status = ? ORDER BY created_at",
            ("approved",),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan

    async def test_notifications_forwarder_uses_index(self, storage: SQLiteStorageAdapter) -> None:
        # cli/operator's forwarder polls WHERE forwarded=0.
        plan = await self._eqp(
            storage,
            "SELECT * FROM notifications WHERE forwarded = 0 " "ORDER BY created_at LIMIT ?",
            (50,),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan

    async def test_llm_calls_24h_window_uses_index(self, storage: SQLiteStorageAdapter) -> None:
        # services/llm_cost_gate's sliding-window cost total.
        plan = await self._eqp(
            storage,
            "SELECT cost_usd FROM llm_calls WHERE timestamp >= ?",
            ("2026-01-01T00:00:00+00:00",),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan

    async def test_price_snapshots_by_symbol_time_uses_index(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # cli/maintenance's prune query reads observed_at < cutoff.
        plan = await self._eqp(
            storage,
            "SELECT * FROM price_snapshots WHERE symbol_base=? "
            "AND symbol_quote=? AND observed_at >= ? ORDER BY observed_at",
            ("BTC", "USD", "2026-01-01T00:00:00+00:00"),
        )
        assert "SEARCH" in plan
        assert "SCAN" not in plan


class TestOrders:
    async def test_save_and_get_order(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order()
        await storage.save_order(order)
        loaded = await storage.get_order(order.id)
        assert loaded is not None
        assert loaded.id == order.id
        assert loaded.symbol == order.symbol
        assert loaded.price.amount == order.price.amount
        assert loaded.amount.value == order.amount.value
        assert loaded.status == "pending"
        assert loaded.exchange_id is None

    async def test_get_order_missing_returns_none(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_order(uuid4()) is None

    async def test_save_order_is_upsert(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order()
        await storage.save_order(order)
        order.mark_open("KRAKEN-TXID-123")
        await storage.save_order(order)
        loaded = await storage.get_order(order.id)
        assert loaded is not None
        assert loaded.exchange_id == "KRAKEN-TXID-123"
        assert loaded.status == "open"
        assert loaded.updated_at is not None

    async def test_decimal_precision_round_trip(self, storage: SQLiteStorageAdapter) -> None:
        order = _make_order(price_amount="12345.12345678", amount_value="0.00012345")
        await storage.save_order(order)
        loaded = await storage.get_order(order.id)
        assert loaded is not None
        assert loaded.price.amount == Decimal("12345.12345678")
        assert loaded.amount.value == Decimal("0.00012345")

    async def test_get_open_orders_excludes_terminal_states(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        pending = _make_order(status="pending")
        open_order = _make_order(status="open")
        open_order.exchange_id = "TX-OPEN"
        closed = _make_order(status="closed")
        canceled = _make_order(status="canceled")
        for o in (pending, open_order, closed, canceled):
            await storage.save_order(o)

        open_ids = {o.id for o in await storage.get_open_orders()}
        assert open_ids == {pending.id, open_order.id}

    async def test_get_open_orders_filtered_by_symbol(self, storage: SQLiteStorageAdapter) -> None:
        btc = _make_order(symbol=Symbol(base="BTC", quote="USD"))
        eth = _make_order(symbol=Symbol(base="ETH", quote="USD"))
        await storage.save_order(btc)
        await storage.save_order(eth)

        only_eth = await storage.get_open_orders(Symbol(base="ETH", quote="USD"))
        assert [o.id for o in only_eth] == [eth.id]


class TestTrades:
    async def test_save_and_query_trade(self, storage: SQLiteStorageAdapter) -> None:
        trade = _make_trade()
        await storage.save_trade(trade)
        results = await storage.get_trades()
        assert len(results) == 1
        assert results[0].id == trade.id
        assert results[0].fee == Decimal("0.25")
        assert results[0].cost == Decimal("2500.0617")

    async def test_trades_filter_by_symbol(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_trade(
            _make_trade(trade_id="T-BTC", symbol=Symbol(base="BTC", quote="USD"))
        )
        await storage.save_trade(
            _make_trade(trade_id="T-ETH", symbol=Symbol(base="ETH", quote="USD"))
        )

        eth_only = await storage.get_trades(symbol=Symbol(base="ETH", quote="USD"))
        assert [t.id for t in eth_only] == ["T-ETH"]

    async def test_trades_filter_by_time_window(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        await storage.save_trade(_make_trade(trade_id="T1", executed_at=base))
        await storage.save_trade(_make_trade(trade_id="T2", executed_at=base + timedelta(hours=1)))
        await storage.save_trade(_make_trade(trade_id="T3", executed_at=base + timedelta(hours=2)))

        window = await storage.get_trades(
            start_time=base + timedelta(minutes=30),
            end_time=base + timedelta(hours=1, minutes=30),
        )
        assert [t.id for t in window] == ["T2"]

    async def test_trades_returned_newest_first(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        await storage.save_trade(_make_trade(trade_id="T1", executed_at=base))
        await storage.save_trade(_make_trade(trade_id="T2", executed_at=base + timedelta(hours=1)))
        results = await storage.get_trades()
        assert [t.id for t in results] == ["T2", "T1"]

    async def test_trade_limit_clamps_results(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            await storage.save_trade(
                _make_trade(trade_id=f"T{i}", executed_at=base + timedelta(minutes=i))
            )
        results = await storage.get_trades(limit=2)
        assert len(results) == 2


class TestBalanceSnapshots:
    async def test_save_and_load_snapshot(self, storage: SQLiteStorageAdapter) -> None:
        balances = [
            Balance(
                asset="BTC", total=Decimal("1.5"), available=Decimal("1.0"), locked=Decimal("0.5")
            ),
            Balance(
                asset="USD", total=Decimal("1000"), available=Decimal("1000"), locked=Decimal("0")
            ),
        ]
        await storage.save_balance_snapshot(balances)

        loaded = await storage.get_latest_balance_snapshot()
        by_asset = {b.asset: b for b in loaded}
        assert by_asset["BTC"].total == Decimal("1.5")
        assert by_asset["BTC"].locked == Decimal("0.5")
        assert by_asset["USD"].total == Decimal("1000")

    async def test_latest_snapshot_wins(self, storage: SQLiteStorageAdapter) -> None:
        first = [Balance(asset="BTC", total=Decimal("1"), available=Decimal("1"))]
        second = [Balance(asset="BTC", total=Decimal("2"), available=Decimal("2"))]
        await storage.save_balance_snapshot(first)
        await storage.save_balance_snapshot(second)

        loaded = await storage.get_latest_balance_snapshot()
        assert len(loaded) == 1
        assert loaded[0].total == Decimal("2")

    async def test_empty_snapshot_rejected(self, storage: SQLiteStorageAdapter) -> None:
        with pytest.raises(StorageError, match="empty balance snapshot"):
            await storage.save_balance_snapshot([])

    async def test_no_snapshot_returns_empty_list(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_latest_balance_snapshot() == []

    async def test_snapshot_rolls_back_on_duplicate_assets(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A failure mid-snapshot must not leave an orphan header row.

        save_balance_snapshot inserts the snapshot_id header first, then
        bulk-inserts entries. If the entries fail (here: duplicate
        (snapshot_id, asset) primary key), the snapshot header must roll
        back too — otherwise the next call commits both as an orphan.
        """
        await storage.save_balance_snapshot(
            [Balance(asset="BTC", total=Decimal("1"), available=Decimal("1"))]
        )

        # Duplicate asset within one snapshot violates the (snapshot_id, asset) PK
        duplicate = [
            Balance(asset="ETH", total=Decimal("2"), available=Decimal("2")),
            Balance(asset="ETH", total=Decimal("3"), available=Decimal("3")),
        ]
        with pytest.raises(StorageError):
            await storage.save_balance_snapshot(duplicate)

        # Latest snapshot must still be the original BTC one, not an orphan
        latest = await storage.get_latest_balance_snapshot()
        assert len(latest) == 1
        assert latest[0].asset == "BTC"

        # And we must still be able to save a fresh snapshot afterwards
        # (proves the connection's transaction state is clean)
        await storage.save_balance_snapshot(
            [Balance(asset="USD", total=Decimal("100"), available=Decimal("100"))]
        )
        latest = await storage.get_latest_balance_snapshot()
        assert len(latest) == 1
        assert latest[0].asset == "USD"

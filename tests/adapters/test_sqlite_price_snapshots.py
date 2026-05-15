"""SQLiteStorageAdapter tests for the price-snapshot persistence (Stage 3.0)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Price, Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def test_save_single_snapshot(storage: SQLiteStorageAdapter) -> None:
    """A single snapshot lands without error and is queryable."""
    now = Timestamp(dt=datetime.now(UTC))
    await storage.save_price_snapshot(
        BTC_USD, Price(amount=Decimal("81558.10"), currency="USD"), now
    )
    # Direct SQL query — there's no get_recent_prices yet (deferred to 3.1)
    conn = storage._require_conn()  # type: ignore[reportPrivateUsage]
    async with conn.execute(
        "SELECT symbol_base, symbol_quote, price_amount, price_currency, observed_at "
        "FROM price_snapshots"
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["symbol_base"] == "BTC"
    assert rows[0]["price_amount"] == "81558.10"
    assert rows[0]["price_currency"] == "USD"


async def test_decimal_precision_preserved(storage: SQLiteStorageAdapter) -> None:
    """Long-decimal prices round-trip without loss (TEXT storage)."""
    now = Timestamp(dt=datetime.now(UTC))
    await storage.save_price_snapshot(
        BTC_USD, Price(amount=Decimal("81558.123456789"), currency="USD"), now
    )
    conn = storage._require_conn()  # type: ignore[reportPrivateUsage]
    async with conn.execute("SELECT price_amount FROM price_snapshots") as cursor:
        row = await cursor.fetchone()
    assert row["price_amount"] == "81558.123456789"


async def test_multiple_snapshots_appended_with_ordering(
    storage: SQLiteStorageAdapter,
) -> None:
    """The table is append-only; index supports time-ordered queries."""
    base_time = datetime.now(UTC) - timedelta(minutes=10)
    for i in range(5):
        await storage.save_price_snapshot(
            BTC_USD,
            Price(amount=Decimal(f"81558.{i}0"), currency="USD"),
            Timestamp(dt=base_time + timedelta(minutes=i)),
        )
    conn = storage._require_conn()  # type: ignore[reportPrivateUsage]
    async with conn.execute(
        "SELECT price_amount FROM price_snapshots "
        "WHERE symbol_base = 'BTC' AND symbol_quote = 'USD' "
        "ORDER BY observed_at"
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 5
    assert [r["price_amount"] for r in rows] == [
        "81558.00",
        "81558.10",
        "81558.20",
        "81558.30",
        "81558.40",
    ]


async def test_independent_per_symbol(storage: SQLiteStorageAdapter) -> None:
    """Snapshots for different symbols coexist; symbol filter works."""
    eth = Symbol(base="ETH", quote="USD")
    now = Timestamp(dt=datetime.now(UTC))
    await storage.save_price_snapshot(BTC_USD, Price(amount=Decimal("81558"), currency="USD"), now)
    await storage.save_price_snapshot(eth, Price(amount=Decimal("2288"), currency="USD"), now)
    conn = storage._require_conn()  # type: ignore[reportPrivateUsage]
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM price_snapshots WHERE symbol_base = 'ETH'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1


class TestGetPriceSnapshots:
    """Tests for the Stage 3.1 read path ``get_price_snapshots``."""

    async def test_empty_when_no_rows(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_price_snapshots() == []

    async def test_returns_all_when_no_filters(self, storage: SQLiteStorageAdapter) -> None:
        eth = Symbol(base="ETH", quote="USD")
        now = Timestamp(dt=datetime.now(UTC))
        await storage.save_price_snapshot(
            BTC_USD, Price(amount=Decimal("81558.10"), currency="USD"), now
        )
        await storage.save_price_snapshot(
            eth, Price(amount=Decimal("2288.00"), currency="USD"), now
        )
        result = await storage.get_price_snapshots()
        assert len(result) == 2
        assert {snap.symbol.base for snap in result} == {"BTC", "ETH"}

    async def test_symbol_filter(self, storage: SQLiteStorageAdapter) -> None:
        eth = Symbol(base="ETH", quote="USD")
        now = Timestamp(dt=datetime.now(UTC))
        await storage.save_price_snapshot(
            BTC_USD, Price(amount=Decimal("81558.10"), currency="USD"), now
        )
        await storage.save_price_snapshot(
            eth, Price(amount=Decimal("2288.00"), currency="USD"), now
        )
        result = await storage.get_price_snapshots(symbol=BTC_USD)
        assert len(result) == 1
        assert result[0].symbol == BTC_USD
        assert result[0].price.amount == Decimal("81558.10")

    async def test_ordered_by_observed_at_ascending(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime.now(UTC) - timedelta(minutes=10)
        # Insert out-of-order so we know ORDER BY is doing work
        offsets = [3, 1, 4, 0, 2]
        for off in offsets:
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(f"81558.{off}0"), currency="USD"),
                Timestamp(dt=base + timedelta(minutes=off)),
            )
        result = await storage.get_price_snapshots(symbol=BTC_USD)
        times = [snap.observed_at.dt for snap in result]
        assert times == sorted(times)

    async def test_start_time_inclusive(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime.now(UTC) - timedelta(minutes=10)
        for i in range(5):
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(f"81558.{i}0"), currency="USD"),
                Timestamp(dt=base + timedelta(minutes=i)),
            )
        # Boundary inclusive: filter at the third point should return 3 rows
        cutoff = base + timedelta(minutes=2)
        result = await storage.get_price_snapshots(symbol=BTC_USD, start_time=cutoff)
        assert len(result) == 3
        assert all(snap.observed_at.dt >= cutoff for snap in result)

    async def test_end_time_inclusive(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime.now(UTC) - timedelta(minutes=10)
        for i in range(5):
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(f"81558.{i}0"), currency="USD"),
                Timestamp(dt=base + timedelta(minutes=i)),
            )
        cutoff = base + timedelta(minutes=2)
        result = await storage.get_price_snapshots(symbol=BTC_USD, end_time=cutoff)
        assert len(result) == 3
        assert all(snap.observed_at.dt <= cutoff for snap in result)

    async def test_time_window(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime.now(UTC) - timedelta(minutes=10)
        for i in range(5):
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(f"81558.{i}0"), currency="USD"),
                Timestamp(dt=base + timedelta(minutes=i)),
            )
        result = await storage.get_price_snapshots(
            symbol=BTC_USD,
            start_time=base + timedelta(minutes=1),
            end_time=base + timedelta(minutes=3),
        )
        assert len(result) == 3
        amounts = [snap.price.amount for snap in result]
        assert amounts == [Decimal("81558.10"), Decimal("81558.20"), Decimal("81558.30")]

    async def test_limit_caps_rows(self, storage: SQLiteStorageAdapter) -> None:
        base = datetime.now(UTC) - timedelta(minutes=10)
        for i in range(5):
            await storage.save_price_snapshot(
                BTC_USD,
                Price(amount=Decimal(f"81558.{i}0"), currency="USD"),
                Timestamp(dt=base + timedelta(minutes=i)),
            )
        result = await storage.get_price_snapshots(symbol=BTC_USD, limit=2)
        assert len(result) == 2
        # Limit takes the *first* two by observed_at ASC, not arbitrary rows
        assert result[0].price.amount == Decimal("81558.00")
        assert result[1].price.amount == Decimal("81558.10")

    async def test_decimal_precision_preserved_through_read(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Long-decimal prices round-trip across the read path too."""
        now = Timestamp(dt=datetime.now(UTC))
        await storage.save_price_snapshot(
            BTC_USD,
            Price(amount=Decimal("81558.123456789"), currency="USD"),
            now,
        )
        result = await storage.get_price_snapshots(symbol=BTC_USD)
        assert result[0].price.amount == Decimal("81558.123456789")

    async def test_nonmatching_symbol_returns_empty(self, storage: SQLiteStorageAdapter) -> None:
        now = Timestamp(dt=datetime.now(UTC))
        await storage.save_price_snapshot(
            BTC_USD, Price(amount=Decimal("81558"), currency="USD"), now
        )
        doge = Symbol(base="DOGE", quote="USD")
        assert await storage.get_price_snapshots(symbol=doge) == []

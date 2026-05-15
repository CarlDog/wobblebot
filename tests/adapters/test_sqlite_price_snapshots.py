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

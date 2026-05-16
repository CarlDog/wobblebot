"""SQLiteStorageAdapter tests for transfer_results persistence (Stage 4.4b)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferResult

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _result(
    *,
    proposal_id: str = "prop-1",
    transaction_id: str | None = None,
    status: str = "completed",
    amount: str = "100",
    direction: str = "exchange_to_bank",
    asset: str = "USD",
    minutes_ago: int = 1,
) -> TransferResult:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return TransferResult(
        proposal_id=proposal_id,
        transaction_id=transaction_id or f"tx-{uuid4()}",
        status=status,  # type: ignore[arg-type]
        executed_amount=Decimal(amount),
        direction=direction,  # type: ignore[arg-type]
        asset=asset,
        timestamp=Timestamp(dt=when),
    )


async def test_save_then_read(storage: SQLiteStorageAdapter) -> None:
    result = _result(transaction_id="AGBSO6T-UFMTTQ-I7KGS6")
    await storage.save_transfer_result(result)
    got = await storage.get_transfer_results()
    assert len(got) == 1
    row = got[0]
    assert row.transaction_id == "AGBSO6T-UFMTTQ-I7KGS6"
    assert row.status == "completed"
    assert row.direction == "exchange_to_bank"
    assert row.asset == "USD"
    assert row.executed_amount == Decimal("100")


async def test_decimal_precision_round_trips(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_result(_result(amount="225.0040"))
    row = (await storage.get_transfer_results())[0]
    assert row.executed_amount == Decimal("225.0040")


async def test_default_order_is_timestamp_desc(storage: SQLiteStorageAdapter) -> None:
    for offset, tag in [(30, "old"), (10, "mid"), (1, "new")]:
        await storage.save_transfer_result(
            _result(transaction_id=f"tx-{tag}", minutes_ago=offset),
        )
    got = await storage.get_transfer_results()
    assert [r.transaction_id for r in got] == ["tx-new", "tx-mid", "tx-old"]


async def test_status_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_result(_result(transaction_id="tx-p", status="pending"))
    await storage.save_transfer_result(_result(transaction_id="tx-c", status="completed"))
    await storage.save_transfer_result(_result(transaction_id="tx-f", status="failed"))
    got = await storage.get_transfer_results(status="failed")
    assert len(got) == 1
    assert got[0].status == "failed"


async def test_direction_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_result(
        _result(transaction_id="tx-out", direction="exchange_to_bank"),
    )
    await storage.save_transfer_result(
        _result(transaction_id="tx-in", direction="bank_to_exchange"),
    )
    got = await storage.get_transfer_results(direction="exchange_to_bank")
    assert len(got) == 1
    assert got[0].direction == "exchange_to_bank"


async def test_asset_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_result(_result(transaction_id="tx-usd", asset="USD"))
    await storage.save_transfer_result(_result(transaction_id="tx-eur", asset="EUR"))
    got = await storage.get_transfer_results(asset="USD")
    assert len(got) == 1


async def test_since_filter_inclusive(storage: SQLiteStorageAdapter) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for off, tag in [(0, "oldest"), (30, "mid"), (60, "newer"), (90, "newest")]:
        await storage.save_transfer_result(
            TransferResult(
                proposal_id="p",
                transaction_id=f"tx-{tag}",
                status="completed",
                executed_amount=Decimal("10"),
                direction="exchange_to_bank",
                asset="USD",
                timestamp=Timestamp(dt=base + timedelta(minutes=off)),
            ),
        )
    cutoff = base + timedelta(minutes=30)
    got = await storage.get_transfer_results(since=cutoff)
    assert len(got) == 3
    assert all(r.timestamp.dt >= cutoff for r in got)


async def test_limit_caps_rows(storage: SQLiteStorageAdapter) -> None:
    for i in range(5):
        await storage.save_transfer_result(
            _result(transaction_id=f"tx-{i}", minutes_ago=i),
        )
    got = await storage.get_transfer_results(limit=2)
    assert len(got) == 2


async def test_empty_returns_empty_list(storage: SQLiteStorageAdapter) -> None:
    assert await storage.get_transfer_results() == []


async def test_duplicate_transaction_id_rejected(storage: SQLiteStorageAdapter) -> None:
    """transaction_id is UNIQUE — a retry must not silently double-insert."""
    result = _result(transaction_id="tx-unique")
    await storage.save_transfer_result(result)
    with pytest.raises(StorageError, match="tx-unique"):
        await storage.save_transfer_result(result)

"""ledger_entries persistence (ADR-040 follow-up)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import LedgerEntry
from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.retention import FORENSIC_TABLES


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _entry(
    eid: str,
    asset: str = "SOL",
    etype: str = "staking",
    amount: str = "0.001",
    fee: str = "0.0003",
    when: datetime | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        id=eid,
        ref_id=f"R-{eid}",
        asset=asset,
        entry_type=etype,
        amount=Decimal(amount),
        fee=Decimal(fee),
        occurred_at=Timestamp(dt=when or datetime(2026, 8, 22, 12, 0, tzinfo=UTC)),
    )


@pytest.mark.asyncio
class TestLedgerRoundTrip:
    async def test_decimals_survive_exactly(self, storage: SQLiteStorageAdapter) -> None:
        """Stored as TEXT precisely so a reward like 0.0019579684 does
        not become a float. The 30% fee reconciliation closed to the
        last digit; float would destroy that."""
        await storage.save_ledger_entries([_entry("L1", amount="0.0019579684", fee="0.0005873901")])
        (got,) = await storage.get_ledger_entries()
        assert got.amount == Decimal("0.0019579684")
        assert got.fee == Decimal("0.0005873901")
        assert got.net_amount == Decimal("0.0013705783")

    async def test_reingest_is_idempotent(self, storage: SQLiteStorageAdapter) -> None:
        """The ingest re-fetches a fixed window every cycle instead of
        keeping a watermark. That is only safe if re-writing the same
        ids cannot double-count income."""
        batch = [_entry("L1"), _entry("L2")]
        await storage.save_ledger_entries(batch)
        await storage.save_ledger_entries(batch)
        assert len(await storage.get_ledger_entries()) == 2

    async def test_empty_batch_is_a_noop(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.save_ledger_entries([]) == 0

    async def test_filters_by_asset_and_type(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_ledger_entries(
            [
                _entry("L1", asset="SOL", etype="staking"),
                _entry("L2", asset="ADA", etype="staking"),
                _entry("L3", asset="USD", etype="deposit"),
            ]
        )
        assert len(await storage.get_ledger_entries(asset="SOL")) == 1
        assert len(await storage.get_ledger_entries(entry_type="staking")) == 2
        assert len(await storage.get_ledger_entries(entry_type="deposit")) == 1

    async def test_newest_first(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_ledger_entries(
            [
                _entry("old", when=datetime(2026, 1, 1, tzinfo=UTC)),
                _entry("new", when=datetime(2026, 8, 1, tzinfo=UTC)),
            ]
        )
        got = await storage.get_ledger_entries()
        assert [e.id for e in got] == ["new", "old"]

    async def test_unknown_type_is_storable(self, storage: SQLiteStorageAdapter) -> None:
        """No CHECK constraint on entry_type, deliberately — a reward
        type Kraken adds later must land in the table as itself rather
        than being rejected at the DB layer."""
        await storage.save_ledger_entries([_entry("L1", etype="some_future_reward")])
        (got,) = await storage.get_ledger_entries()
        assert got.entry_type == "some_future_reward"


def test_ledger_entries_is_forensic() -> None:
    """Income records must never be pruned — the trades table cannot
    reconstruct them."""
    assert "ledger_entries" in FORENSIC_TABLES

"""Tests for services.maintenance (Stage 8.2.B)."""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import PriceSnapshot
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.services.maintenance import (
    archive_price_snapshots_to_csv,
    prune_price_snapshots,
    vacuum_database,
)

pytestmark = pytest.mark.unit


def _snapshot(*, hours_ago: float = 1.0, symbol: str = "BTC/USD") -> PriceSnapshot:
    base, quote = symbol.split("/")
    return PriceSnapshot(
        symbol=Symbol(base=base, quote=quote),
        price=Price(amount=Decimal("30000"), currency="USD"),
        observed_at=Timestamp(dt=datetime.now(UTC) - timedelta(hours=hours_ago)),
    )


async def _save(storage: SQLiteStorageAdapter, snap: PriceSnapshot) -> None:
    """Adapter shim — storage.save_price_snapshot takes individual fields."""
    await storage.save_price_snapshot(snap.symbol, snap.price, snap.observed_at)


# --------------------------------------------------------------------- #
# vacuum_database                                                       #
# --------------------------------------------------------------------- #


class TestVacuumDatabase:
    @pytest.mark.asyncio
    async def test_runs_on_real_db_file(self, tmp_path: Path) -> None:
        """Smoke test against a real SQLite file."""
        db_path = tmp_path / "test.db"
        storage = SQLiteStorageAdapter(str(db_path))
        await storage.connect()
        # Insert some data so VACUUM has something to compact.
        await _save(storage, _snapshot(hours_ago=1))
        await storage.delete_price_snapshots(before=datetime.now(UTC))
        await storage.close()
        # File must exist + be closed before VACUUM.
        size_before = db_path.stat().st_size
        vacuum_database(db_path)
        size_after = db_path.stat().st_size
        # Size shouldn't grow; usually shrinks. Just check no
        # corruption (file still readable).
        assert size_after <= size_before
        # Re-open to verify the DB is still valid.
        storage = SQLiteStorageAdapter(str(db_path))
        await storage.connect()
        await storage.close()

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            vacuum_database(tmp_path / "nope.db")


# --------------------------------------------------------------------- #
# archive_price_snapshots_to_csv                                        #
# --------------------------------------------------------------------- #


class TestArchivePriceSnapshotsToCsv:
    def test_writes_header_and_rows(self, tmp_path: Path) -> None:
        snaps = [_snapshot(hours_ago=h) for h in (1, 2, 3)]
        dest = tmp_path / "obs.csv"
        count = archive_price_snapshots_to_csv(snaps, dest)
        assert count == 3
        with dest.open(encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == [
            "observed_at",
            "symbol_base",
            "symbol_quote",
            "price",
            "currency",
        ]
        assert len(rows) == 4  # header + 3 data rows
        # All 3 rows have BTC/USD/30000
        for row in rows[1:]:
            assert row[1] == "BTC"
            assert row[2] == "USD"
            assert row[3] == "30000"
            assert row[4] == "USD"

    def test_empty_input_writes_header_only(self, tmp_path: Path) -> None:
        dest = tmp_path / "empty.csv"
        count = archive_price_snapshots_to_csv([], dest)
        assert count == 0
        with dest.open(encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # header only

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        dest = tmp_path / "existing.csv"
        dest.write_text("preexisting content")
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            archive_price_snapshots_to_csv([_snapshot()], dest)
        # Original content preserved
        assert dest.read_text() == "preexisting content"

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "a" / "b" / "c" / "obs.csv"
        archive_price_snapshots_to_csv([_snapshot()], dest)
        assert dest.exists()


# --------------------------------------------------------------------- #
# prune_price_snapshots                                                 #
# --------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest.mark.asyncio
class TestPrunePriceSnapshots:
    async def test_archives_and_deletes_eligible_rows(
        self, storage: SQLiteStorageAdapter, tmp_path: Path
    ) -> None:
        # Seed 5 old + 3 fresh snapshots.
        for h in (48, 36, 24, 12, 6):
            await _save(storage, _snapshot(hours_ago=h))
        for h in (1, 0.5, 0.1):
            await _save(storage, _snapshot(hours_ago=h))

        cutoff = datetime.now(UTC) - timedelta(hours=4)
        count = await prune_price_snapshots(
            storage,
            older_than=cutoff,
            archive_dir=tmp_path,
            archive_name="obs-2026-05-18.csv",
        )
        # 5 old rows eligible (>4h ago); 3 fresh rows survive
        assert count == 5
        # CSV exists with the archived rows
        archive = tmp_path / "obs-2026-05-18.csv"
        assert archive.exists()
        with archive.open(encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 6  # header + 5 rows
        # Remaining snapshots in storage
        remaining = await storage.get_price_snapshots()
        assert len(remaining) == 3

    async def test_no_eligible_rows_is_noop(
        self, storage: SQLiteStorageAdapter, tmp_path: Path
    ) -> None:
        # All snapshots are fresh.
        for h in (1, 0.5):
            await _save(storage, _snapshot(hours_ago=h))
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        count = await prune_price_snapshots(
            storage,
            older_than=cutoff,
            archive_dir=tmp_path,
            archive_name="obs.csv",
        )
        assert count == 0
        # No archive file created.
        assert not (tmp_path / "obs.csv").exists()
        # Storage unchanged.
        assert len(await storage.get_price_snapshots()) == 2

    async def test_archive_failure_leaves_rows_intact(
        self, storage: SQLiteStorageAdapter, tmp_path: Path
    ) -> None:
        """If the archive write fails (e.g. file exists), rows MUST
        stay in storage. Archive-then-delete discipline."""
        for h in (48, 24):
            await _save(storage, _snapshot(hours_ago=h))
        archive = tmp_path / "obs.csv"
        archive.write_text("pre-existing")  # collision forces failure

        cutoff = datetime.now(UTC) - timedelta(hours=4)
        with pytest.raises(FileExistsError):
            await prune_price_snapshots(
                storage,
                older_than=cutoff,
                archive_dir=tmp_path,
                archive_name="obs.csv",
            )
        # Rows untouched (DELETE never ran).
        remaining = await storage.get_price_snapshots()
        assert len(remaining) == 2

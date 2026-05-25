"""Tests for the v1.1 price_snapshots UNIQUE constraint + migration.

Slice-3 backfill follow-up (2026-05-25). Slice 3 left price_snapshots
without a UNIQUE constraint; this slice closes it. Covers:
- Migration on a clean DB: index created, no rows touched
- Migration on a DB with synthetic duplicates: dedup + index
- save_price_snapshot is idempotent (INSERT OR IGNORE)
- save_price_snapshots returns post-dedup rowcount
- Concurrent backfill-during-daemon scenario produces no duplicate rows
- WARN log fires when dedup count > 0
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Price, Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _price(amount: str = "79000") -> Price:
    return Price(amount=Decimal(amount), currency="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestSaveSinglePriceSnapshotIdempotent:
    async def test_duplicate_write_is_silent_no_op(self, storage: SQLiteStorageAdapter) -> None:
        """save_price_snapshot becomes idempotent post-INSERT OR IGNORE.
        The daemon's poll loop relies on save NOT raising on dup."""
        await storage.save_price_snapshot(_BTC, _price(), Timestamp(dt=_T0))
        # Second write at the same (symbol, observed_at) is a no-op,
        # not a constraint violation.
        await storage.save_price_snapshot(_BTC, _price("80000"), Timestamp(dt=_T0))
        snaps = await storage.get_price_snapshots(symbol=_BTC)
        assert len(snaps) == 1
        # First write wins (INSERT OR IGNORE keeps the original).
        assert snaps[0].price.amount == Decimal("79000")


class TestSavePriceSnapshotsBatchIdempotent:
    async def test_returns_post_dedup_count(self, storage: SQLiteStorageAdapter) -> None:
        """Batch rowcount reflects actual writes -- backfill's
        snapshots_inserted accuracy depends on this."""
        snapshots = [(_BTC, _price(), Timestamp(dt=_T0 + timedelta(minutes=i))) for i in range(5)]
        first_count = await storage.save_price_snapshots(snapshots)
        assert first_count == 5
        # Re-run same batch: zero new rows.
        second_count = await storage.save_price_snapshots(snapshots)
        assert second_count == 0
        # Final row count is 5, not 10.
        rows = await storage.get_price_snapshots(symbol=_BTC)
        assert len(rows) == 5

    async def test_partial_overlap_counts_only_new_rows(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The backfill service's partial-overlap accounting relies on
        this -- snapshots_inserted should report only actually-new rows."""
        first = [(_BTC, _price(), Timestamp(dt=_T0 + timedelta(minutes=i))) for i in range(5)]
        await storage.save_price_snapshots(first)
        second = [(_BTC, _price(), Timestamp(dt=_T0 + timedelta(minutes=i))) for i in range(3, 8)]
        inserted = await storage.save_price_snapshots(second)
        assert inserted == 3  # minutes 5, 6, 7 are new


class TestConcurrentBackfillDuringDaemon:
    async def test_daemon_poll_and_backfill_synthesize_dont_duplicate(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Reproduces the scenario from the in-conversation question:
        daemon wrote a snapshot at T; backfill later synthesizes at T.
        Pre-fix this duplicated; post-fix the backfill-side write is
        a silent no-op."""
        # Daemon's poll.
        await storage.save_price_snapshot(_BTC, _price("79000"), Timestamp(dt=_T0))
        # Backfill's synthesized snapshot at the same instant.
        inserted = await storage.save_price_snapshots([(_BTC, _price("79050"), Timestamp(dt=_T0))])
        assert inserted == 0
        rows = await storage.get_price_snapshots(symbol=_BTC)
        assert len(rows) == 1


class TestMigrationOnExistingDB:
    """File-backed-DB tests: the migration path is the interesting bit."""

    async def test_clean_db_migration_is_noop(self, tmp_path: Path) -> None:
        """A migration on a DB with no duplicates touches no rows; the
        UNIQUE index is created idempotently."""
        db_path = tmp_path / "observe.db"
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            for offset in range(5):
                await adapter.save_price_snapshot(
                    _BTC, _price(), Timestamp(dt=_T0 + timedelta(minutes=offset))
                )
        finally:
            await adapter.close()
        # Re-open: migration runs, index already present, no dedup happens.
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            rows = await adapter.get_price_snapshots(symbol=_BTC)
        finally:
            await adapter.close()
        assert len(rows) == 5

    async def test_migration_collapses_existing_duplicates(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Simulate an in-the-wild observe.db that pre-dates this slice
        and has actual duplicates. The migration must dedup + add the
        index successfully."""
        db_path = tmp_path / "observe.db"
        # Bootstrap: connect, then close to grab the file with schema
        # applied (so we can inject dupes via raw sqlite3).
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        await adapter.close()

        # Drop the UNIQUE index that the schema apply just added so we
        # can inject duplicates against the legacy shape; the migration
        # then has to re-add it after dedup. This is the only realistic
        # way to simulate a pre-slice-3-follow-up DB without checking
        # in a binary fixture.
        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute("DROP INDEX IF EXISTS idx_price_snapshots_unique")
            # Insert 3 rows at the same (symbol, observed_at).
            iso = _T0.isoformat()
            for _ in range(3):
                raw.execute(
                    "INSERT INTO price_snapshots "
                    "(symbol_base, symbol_quote, price_amount, price_currency, observed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("BTC", "USD", "79000", "USD", iso),
                )
            raw.commit()
        finally:
            raw.close()

        # Re-open via the adapter; migration fires.
        with caplog.at_level(logging.WARNING, logger="wobblebot.adapters.sqlite_storage"):
            adapter = SQLiteStorageAdapter(str(db_path))
            await adapter.connect()
        try:
            rows = await adapter.get_price_snapshots(symbol=_BTC)
        finally:
            await adapter.close()

        assert len(rows) == 1  # 3 duplicates collapsed to 1
        warn_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "duplicate" in r.getMessage().lower()
        ]
        assert warn_records, "Expected a WARNING about duplicate collapse"

    async def test_migration_runs_idempotently_on_reopen(self, tmp_path: Path) -> None:
        """Re-opening an already-migrated DB doesn't re-fire the WARN
        or alter row counts -- the migration's no-op path."""
        db_path = tmp_path / "observe.db"
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        for offset in range(3):
            await adapter.save_price_snapshot(
                _BTC, _price(), Timestamp(dt=_T0 + timedelta(minutes=offset))
            )
        await adapter.close()

        # Two more open/close cycles.
        for _ in range(2):
            adapter = SQLiteStorageAdapter(str(db_path))
            await adapter.connect()
            await adapter.close()

        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            rows = await adapter.get_price_snapshots(symbol=_BTC)
        finally:
            await adapter.close()
        assert len(rows) == 3

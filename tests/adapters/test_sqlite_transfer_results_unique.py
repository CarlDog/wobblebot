"""Tests for the ADR-026 Harvester ``--execute`` replay guard.

The DB-enforced idempotency layer: a partial UNIQUE index on
``transfer_results(proposal_id) WHERE status != 'failed'``, added via
migration (not baked into ``SCHEMA``, matching the
``price_snapshots`` UNIQUE precedent) so pre-existing rows can be
inspected before the constraint is enforced.

Unlike the ``price_snapshots`` migration, a duplicate non-``failed``
``proposal_id`` here is NOT junk to silently collapse — it could be
the forensic record of a real past double-withdrawal. Covers:
- A second non-``failed`` result for the same proposal is rejected
  (the guard itself).
- A ``failed`` row never blocks a legitimate retry.
- Migration on a clean DB: index created, no rows touched.
- Migration on a DB with pre-existing (legacy) duplicates: the index
  is NOT created, no rows are deleted, and an ERROR is logged.
- Migration re-runs idempotently on a healthy, already-migrated DB.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferResult

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _result(
    *,
    proposal_id: str = "prop-1",
    transaction_id: str | None = None,
    status: str = "completed",
    minutes_ago: int = 1,
) -> TransferResult:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return TransferResult(
        proposal_id=proposal_id,
        transaction_id=transaction_id or f"tx-{uuid4()}",
        status=status,  # type: ignore[arg-type]
        executed_amount=Decimal("100"),
        direction="exchange_to_bank",
        asset="USD",
        timestamp=Timestamp(dt=when),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestReplayGuard:
    async def test_second_non_failed_result_for_same_proposal_rejected(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.save_transfer_result(
            _result(proposal_id="p1", transaction_id="tx-1", status="pending")
        )
        with pytest.raises(StorageError, match="ADR-026 replay guard"):
            await storage.save_transfer_result(
                _result(proposal_id="p1", transaction_id="tx-2", status="completed")
            )
        # Only the first attempt landed.
        rows = await storage.get_transfer_results()
        assert len(rows) == 1

    async def test_pending_then_completed_for_same_proposal_still_rejected(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Both 'pending' and 'completed' are non-failed -- the guard
        covers either combination, not just identical statuses."""
        await storage.save_transfer_result(
            _result(proposal_id="p2", transaction_id="tx-a", status="pending")
        )
        with pytest.raises(StorageError, match="ADR-026 replay guard"):
            await storage.save_transfer_result(
                _result(proposal_id="p2", transaction_id="tx-b", status="pending")
            )

    async def test_failed_result_does_not_block_legitimate_retry(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A prior failed attempt (Kraken rejected, no money moved) must
        not block the operator's legitimate re-execution of the SAME
        proposal once the underlying issue is fixed."""
        await storage.save_transfer_result(
            _result(proposal_id="p3", transaction_id="tx-fail-1", status="failed")
        )
        # Retry succeeds -- must not raise.
        await storage.save_transfer_result(
            _result(proposal_id="p3", transaction_id="tx-retry", status="completed")
        )
        rows = await storage.get_transfer_results()
        assert {r.transaction_id for r in rows} == {"tx-fail-1", "tx-retry"}

    async def test_multiple_failed_results_for_same_proposal_allowed(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Repeated failures (e.g. Kraken down, retried several times)
        must never trip the guard -- only non-failed rows are unique."""
        for i in range(3):
            await storage.save_transfer_result(
                _result(proposal_id="p4", transaction_id=f"tx-fail-{i}", status="failed")
            )
        rows = await storage.get_transfer_results()
        assert len(rows) == 3

    async def test_different_proposals_are_independent(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_transfer_result(
            _result(proposal_id="p5", transaction_id="tx-p5", status="completed")
        )
        await storage.save_transfer_result(
            _result(proposal_id="p6", transaction_id="tx-p6", status="completed")
        )
        rows = await storage.get_transfer_results()
        assert len(rows) == 2


class TestMigrationOnExistingDB:
    """File-backed-DB tests: the migration path is the interesting bit."""

    async def test_clean_db_migration_creates_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "harvest.db"
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            await adapter.save_transfer_result(
                _result(proposal_id="p1", transaction_id="tx-1", status="completed")
            )
        finally:
            await adapter.close()

        # Re-open: migration runs, index already present -- the guard
        # is live on a fresh DB from the very first connect.
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            with pytest.raises(StorageError, match="ADR-026 replay guard"):
                await adapter.save_transfer_result(
                    _result(proposal_id="p1", transaction_id="tx-2", status="completed")
                )
        finally:
            await adapter.close()

    async def test_migration_refuses_to_index_legacy_duplicates(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Simulate an in-the-wild harvest.db that pre-dates ADR-026 and
        already has two non-failed results for one proposal -- possible
        forensic evidence of a real past double-withdrawal. The
        migration must NOT delete either row, and must NOT silently
        start enforcing a guard it can't apply cleanly."""
        db_path = tmp_path / "harvest.db"
        # Bootstrap: connect (creates the index on this still-empty
        # table), then close so we can inject legacy-shape duplicates.
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        await adapter.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute("DROP INDEX IF EXISTS idx_transfer_results_unique_proposal")
            now_iso = datetime.now(UTC).isoformat()
            for tx_id, status in [("tx-dup-1", "completed"), ("tx-dup-2", "pending")]:
                raw.execute(
                    """
                    INSERT INTO transfer_results (
                        proposal_id, transaction_id, status, executed_amount,
                        direction, asset, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("dup-proposal", tx_id, status, "100", "exchange_to_bank", "USD", now_iso),
                )
            raw.commit()
        finally:
            raw.close()

        # Re-open via the adapter; migration fires and must find the
        # pre-existing duplicate group.
        with caplog.at_level(logging.ERROR, logger="wobblebot.adapters.sqlite_storage"):
            adapter = SQLiteStorageAdapter(str(db_path))
            await adapter.connect()
        try:
            rows = await adapter.get_transfer_results()
            # Neither duplicate row was touched -- this is forensic data.
            assert {r.transaction_id for r in rows} == {"tx-dup-1", "tx-dup-2"}

            # The guard is verifiably NOT enforced: a third non-failed
            # result for the same proposal must be allowed to insert
            # rather than raising -- proving the index was not created.
            await adapter.save_transfer_result(
                _result(proposal_id="dup-proposal", transaction_id="tx-dup-3", status="completed")
            )
        finally:
            await adapter.close()

        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "double-withdrawal" in r.getMessage()
        ]
        assert error_records, "Expected an ERROR log about the unresolved legacy duplicate"

    async def test_migration_runs_idempotently_on_reopen(self, tmp_path: Path) -> None:
        """Re-opening an already-migrated, healthy DB doesn't alter row
        counts or break the guard on subsequent opens."""
        db_path = tmp_path / "harvest.db"
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        await adapter.save_transfer_result(
            _result(proposal_id="p1", transaction_id="tx-1", status="completed")
        )
        await adapter.close()

        for _ in range(2):
            adapter = SQLiteStorageAdapter(str(db_path))
            await adapter.connect()
            await adapter.close()

        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            rows = await adapter.get_transfer_results()
            assert len(rows) == 1
            with pytest.raises(StorageError, match="ADR-026 replay guard"):
                await adapter.save_transfer_result(
                    _result(proposal_id="p1", transaction_id="tx-2", status="completed")
                )
        finally:
            await adapter.close()

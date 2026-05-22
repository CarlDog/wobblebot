"""Stage 8.4.E follow-up — tests for the daemon_heartbeats table."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestUpsertAndRead:
    async def test_empty_db_returns_empty_map(self, storage: SQLiteStorageAdapter) -> None:
        beats = await storage.get_daemon_heartbeats()
        assert beats == {}

    async def test_single_insert_and_read(self, storage: SQLiteStorageAdapter) -> None:
        ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        await storage.upsert_daemon_heartbeat("cli/live", ts)
        beats = await storage.get_daemon_heartbeats()
        assert beats == {"cli/live": ts}

    async def test_multiple_daemons(self, storage: SQLiteStorageAdapter) -> None:
        ts1 = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 5, 22, 12, 1, 0, tzinfo=UTC)
        await storage.upsert_daemon_heartbeat("cli/live", ts1)
        await storage.upsert_daemon_heartbeat("cli/harvest", ts2)
        beats = await storage.get_daemon_heartbeats()
        assert beats == {"cli/live": ts1, "cli/harvest": ts2}

    async def test_upsert_overwrites_previous(self, storage: SQLiteStorageAdapter) -> None:
        """Each upsert should refresh the row, not append a new one."""
        ts1 = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 5, 22, 12, 0, 5, tzinfo=UTC)
        await storage.upsert_daemon_heartbeat("cli/live", ts1)
        await storage.upsert_daemon_heartbeat("cli/live", ts2)
        beats = await storage.get_daemon_heartbeats()
        assert beats == {"cli/live": ts2}

    async def test_non_utc_input_normalized_on_read(self, storage: SQLiteStorageAdapter) -> None:
        """Caller may pass a tz-aware datetime in any zone; we round-trip
        through ISO and re-attach UTC on read (the stored value is the
        UTC instant regardless of source tz)."""
        non_utc = datetime(2026, 5, 22, 7, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        await storage.upsert_daemon_heartbeat("cli/observe", non_utc)
        beats = await storage.get_daemon_heartbeats()
        # 7 AM US Central -> noon UTC
        assert beats["cli/observe"] == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


class TestSchemaConstraints:
    async def test_empty_name_rejected_by_check_constraint(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.ports.exceptions import StorageError

        with pytest.raises(StorageError):
            await storage.upsert_daemon_heartbeat("", datetime.now(UTC))

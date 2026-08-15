"""``notifications.read_at`` migration + read-state semantics (P3 slice 19).

The trap this guards: SCHEMA runs via ``executescript`` BEFORE any
migration, so the unread partial index cannot live in SCHEMA — an
operator DB written before this slice has no ``read_at`` column and
``CREATE INDEX ... ON notifications(read_at)`` would abort the whole
connect(). The index is created by ``migrate_notifications_read_at``
*after* the ALTER, and these tests pin both halves against a genuinely
pre-slice table.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.notifier import Notification

pytestmark = pytest.mark.unit

_PRE_SLICE_DDL = """
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error', 'critical')),
    title           TEXT NOT NULL,
    message         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    context_json    TEXT NOT NULL DEFAULT '{}',
    forwarded       INTEGER NOT NULL DEFAULT 0 CHECK (forwarded IN (0, 1)),
    forwarded_at    TEXT,
    created_at      TEXT NOT NULL
)
"""


def _write_pre_slice_db(db_path: Path, *, titles: tuple[str, ...]) -> None:
    """Create a notifications table exactly as it stood before this slice."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_SLICE_DDL)
    for title in titles:
        conn.execute(
            "INSERT INTO notifications "
            "(level, title, message, timestamp, context_json, forwarded, "
            "forwarded_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "info",
                title,
                "legacy row",
                "2026-08-01T00:00:00+00:00",
                "{}",
                1,
                "2026-08-01T00:00:01+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()


def _notification(title: str) -> Notification:
    return Notification(
        level="info",
        title=title,
        message="…",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


class TestReadAtMigration:
    @pytest.mark.asyncio
    async def test_legacy_rows_migrate_and_read_back_as_unread(self, tmp_path: Path) -> None:
        """Pre-slice rows land on NULL — nobody could acknowledge them."""
        db_path = tmp_path / "legacy.db"
        _write_pre_slice_db(db_path, titles=("old-a", "old-b"))

        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            rows = await adapter.get_notifications()
            assert len(rows) == 2
            assert all(row.read_at is None for row in rows)
            assert await adapter.count_unread_notifications() == 2
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_unread_index_is_created_on_a_migrated_db(self, tmp_path: Path) -> None:
        """The index must exist even though SCHEMA can't declare it.

        SCHEMA is executed before the migrations, so a pre-slice DB
        would blow up on an index over a column that isn't there yet.
        """
        db_path = tmp_path / "legacy.db"
        _write_pre_slice_db(db_path, titles=("old-a",))

        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            conn = adapter._require_conn()  # pylint: disable=protected-access
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_notifications_unread",),
            ) as cursor:
                assert await cursor.fetchone() is not None
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_migration_is_idempotent_across_reopens(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        _write_pre_slice_db(db_path, titles=("old-a",))
        for _ in range(3):
            adapter = SQLiteStorageAdapter(str(db_path))
            await adapter.connect()
            await adapter.close()
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        try:
            assert await adapter.count_unread_notifications() == 1
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_read_state_survives_a_reopen(self, tmp_path: Path) -> None:
        """Acknowledgement is durable — that's the whole point of moving
        it off browser localStorage."""
        db_path = tmp_path / "operator.db"
        adapter = SQLiteStorageAdapter(str(db_path))
        await adapter.connect()
        await adapter.save_notification(_notification("evt"))
        rows = await adapter.get_notifications()
        await adapter.mark_notifications_read([rows[0].id], Timestamp(dt=datetime.now(UTC)))
        await adapter.close()

        reopened = SQLiteStorageAdapter(str(db_path))
        await reopened.connect()
        try:
            assert await reopened.count_unread_notifications() == 0
            assert (await reopened.get_notifications())[0].read_at is not None
        finally:
            await reopened.close()


class TestReadStateSemantics:
    @pytest.mark.asyncio
    async def test_read_at_is_independent_of_forwarded(self, tmp_path: Path) -> None:
        """ "Discord got it" and "a human dismissed it" are different facts."""
        adapter = SQLiteStorageAdapter(str(tmp_path / "operator.db"))
        await adapter.connect()
        try:
            await adapter.save_notification(_notification("evt"))
            row = (await adapter.get_notifications())[0]
            await adapter.mark_notification_forwarded(row.id, Timestamp(dt=datetime.now(UTC)))
            after = (await adapter.get_notifications())[0]
            assert after.forwarded is True
            assert after.read_at is None
            assert await adapter.count_unread_notifications() == 1
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_mark_all_returns_only_newly_read_rows(self, tmp_path: Path) -> None:
        adapter = SQLiteStorageAdapter(str(tmp_path / "operator.db"))
        await adapter.connect()
        try:
            for i in range(3):
                await adapter.save_notification(_notification(f"evt-{i}"))
            rows = await adapter.get_notifications()
            await adapter.mark_notifications_read([rows[0].id], Timestamp(dt=datetime.now(UTC)))
            assert await adapter.mark_all_notifications_read(Timestamp(dt=datetime.now(UTC))) == 2
            assert await adapter.mark_all_notifications_read(Timestamp(dt=datetime.now(UTC))) == 0
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_unknown_id_is_not_an_error(self, tmp_path: Path) -> None:
        """A row pruned between render and click satisfies the intent."""
        adapter = SQLiteStorageAdapter(str(tmp_path / "operator.db"))
        await adapter.connect()
        try:
            assert (
                await adapter.mark_notifications_read([9999], Timestamp(dt=datetime.now(UTC))) == 0
            )
        finally:
            await adapter.close()

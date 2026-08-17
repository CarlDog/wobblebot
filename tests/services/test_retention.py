"""Tests for services.retention (ADR-036).

Seeding goes through the real ``SQLiteStorageAdapter`` wherever the
timestamp is a model field (news_items / conversation_turns) so the
stored ISO format is exactly what production writes; notifications set
``created_at`` adapter-side at save time, so old rows are seeded with a
raw INSERT mirroring the adapter's isoformat convention.
"""

from __future__ import annotations

import csv
import gzip
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.assistant import ConversationTurn
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.retention import (
    FORENSIC_TABLES,
    PRUNABLE_TABLES,
    prune_table,
    write_rows_to_csv_gz,
)

pytestmark = pytest.mark.unit

_NOW = datetime.now(UTC)


def _news_item(*, days_ago: float, external_id: str) -> NewsItem:
    when = Timestamp(dt=_NOW - timedelta(days=days_ago))
    return NewsItem(
        source="rss:test",
        external_id=external_id,
        published_at=when,
        headline=f"headline {external_id}",
        fetched_at=when,
    )


def _turn(*, days_ago: float) -> ConversationTurn:
    return ConversationTurn(
        id=uuid4(),
        channel_id="chan",
        user_id="user",
        role="operator",
        content=f"content from {days_ago} days ago",
        timestamp=Timestamp(dt=_NOW - timedelta(days=days_ago)),
    )


async def _make_db(tmp_path: Path, name: str) -> Path:
    """Create a real-schema DB file and return its path."""
    db_path = tmp_path / name
    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    await storage.close()
    return db_path


def _insert_notification(db_path: Path, *, days_ago: float, title: str) -> None:
    created_at = (_NOW - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO notifications (
                level, title, message, timestamp,
                context_json, forwarded, forwarded_at, created_at
            ) VALUES ('info', ?, 'msg', ?, '{}', 0, NULL, ?)
            """,
            (title, created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Registry pins                                                         #
# --------------------------------------------------------------------- #


class TestRegistry:
    def test_forensic_tables_never_prunable(self) -> None:
        """ADR-036 decision 1 — the two sets stay disjoint, and the
        money-critical names are actually in the forensic set."""
        assert not FORENSIC_TABLES & set(PRUNABLE_TABLES)
        for critical in ("trades", "orders", "transfer_proposals", "pending_commands"):
            assert critical in FORENSIC_TABLES

    @pytest.mark.asyncio
    async def test_registry_columns_exist_in_schema(self, tmp_path: Path) -> None:
        """Every registry entry's table + ts_column must exist in the
        real schema — guards registry drift against a rename."""
        db_path = await _make_db(tmp_path, "schema.db")
        conn = sqlite3.connect(str(db_path))
        try:
            for table, target in PRUNABLE_TABLES.items():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                assert columns, f"table {table} missing from schema"
                assert target.ts_column in columns, f"{table}.{target.ts_column} missing"
        finally:
            conn.close()


# --------------------------------------------------------------------- #
# write_rows_to_csv_gz                                                  #
# --------------------------------------------------------------------- #


class TestWriteRowsToCsvGz:
    def test_round_trip(self, tmp_path: Path) -> None:
        dest = tmp_path / "a" / "out.csv.gz"
        count = write_rows_to_csv_gz(("x", "y"), [(1, "a"), (2, "b")], dest)
        assert count == 2
        with gzip.open(dest, "rt", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [["x", "y"], ["1", "a"], ["2", "b"]]

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.csv.gz"
        dest.write_bytes(b"preexisting")
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            write_rows_to_csv_gz(("x",), [(1,)], dest)
        assert dest.read_bytes() == b"preexisting"


# --------------------------------------------------------------------- #
# prune_table                                                           #
# --------------------------------------------------------------------- #


class TestPruneTable:
    @pytest.mark.asyncio
    async def test_news_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "news.db"
        storage = SQLiteStorageAdapter(str(db_path))
        await storage.connect()
        for days_ago, ext in ((120.0, "old-1"), (100.0, "old-2"), (5.0, "fresh")):
            await storage.save_news_item(_news_item(days_ago=days_ago, external_id=ext))
        await storage.close()

        deleted = prune_table(
            db_path,
            "news_items",
            older_than=_NOW - timedelta(days=90),
            archive_dir=tmp_path / "archive",
            archive_name="news_items-test.csv.gz",
        )
        assert deleted == 2
        assert _count(db_path, "news_items") == 1
        with gzip.open(tmp_path / "archive" / "news_items-test.csv.gz", "rt") as f:
            rows = list(csv.reader(f))
        # Header matches the real column set; 2 archived data rows.
        assert "fetched_at" in rows[0]
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_conversation_turns_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "op.db"
        # Turns built BEFORE the connection opens: a model validation
        # error then can't leak an open aiosqlite connection.
        turns = [_turn(days_ago=120.0), _turn(days_ago=1.0)]
        storage = SQLiteStorageAdapter(str(db_path))
        await storage.connect()
        try:
            for turn in turns:
                await storage.save_conversation_turn(turn)
        finally:
            await storage.close()
        deleted = prune_table(
            db_path,
            "conversation_turns",
            older_than=_NOW - timedelta(days=90),
            archive_dir=tmp_path / "archive",
            archive_name="conversation_turns-test.csv.gz",
        )
        assert deleted == 1
        assert _count(db_path, "conversation_turns") == 1

    @pytest.mark.asyncio
    async def test_notifications_round_trip(self, tmp_path: Path) -> None:
        db_path = await _make_db(tmp_path, "op.db")
        _insert_notification(db_path, days_ago=120.0, title="old")
        _insert_notification(db_path, days_ago=1.0, title="fresh")
        deleted = prune_table(
            db_path,
            "notifications",
            older_than=_NOW - timedelta(days=90),
            archive_dir=tmp_path / "archive",
            archive_name="notifications-test.csv.gz",
        )
        assert deleted == 1
        assert _count(db_path, "notifications") == 1

    @pytest.mark.asyncio
    async def test_nothing_eligible_writes_no_archive(self, tmp_path: Path) -> None:
        db_path = await _make_db(tmp_path, "op.db")
        _insert_notification(db_path, days_ago=1.0, title="fresh")
        archive_dir = tmp_path / "archive"
        deleted = prune_table(
            db_path,
            "notifications",
            older_than=_NOW - timedelta(days=90),
            archive_dir=archive_dir,
            archive_name="notifications-test.csv.gz",
        )
        assert deleted == 0
        assert not archive_dir.exists()

    def test_unknown_table_raises(self, tmp_path: Path) -> None:
        """Defense in depth — the money ledger can't reach the pruner
        even if CLI boot validation were bypassed."""
        with pytest.raises(ValueError, match="not prunable"):
            prune_table(
                tmp_path / "x.db",
                "trades",
                older_than=_NOW,
                archive_dir=tmp_path,
                archive_name="nope.csv.gz",
            )

    def test_missing_db_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            prune_table(
                tmp_path / "absent.db",
                "news_items",
                older_than=_NOW,
                archive_dir=tmp_path,
                archive_name="x.csv.gz",
            )

    @pytest.mark.asyncio
    async def test_existing_archive_blocks_delete(self, tmp_path: Path) -> None:
        """Archive-then-delete: if the archive can't be written, the
        rows must survive."""
        db_path = await _make_db(tmp_path, "op.db")
        _insert_notification(db_path, days_ago=120.0, title="old")
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        (archive_dir / "notifications-test.csv.gz").write_bytes(b"occupied")
        with pytest.raises(FileExistsError):
            prune_table(
                db_path,
                "notifications",
                older_than=_NOW - timedelta(days=90),
                archive_dir=archive_dir,
                archive_name="notifications-test.csv.gz",
            )
        assert _count(db_path, "notifications") == 1

    @pytest.mark.asyncio
    async def test_corrupt_db_raises_storage_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a sqlite file at all........")
        with pytest.raises(StorageError, match="retention read failed"):
            prune_table(
                db_path,
                "news_items",
                older_than=_NOW,
                archive_dir=tmp_path,
                archive_name="x.csv.gz",
            )

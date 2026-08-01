"""Tests for services.backuper (Stage 8.2.C)."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wobblebot.services.backuper import (
    backup_database_locally,
    find_latest_backup,
    prune_old_backups,
    verify_backup_restoration,
)

pytestmark = pytest.mark.unit


def _make_test_db(path: Path) -> None:
    """Create a tiny SQLite file with one table + one row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# backup_database_locally                                               #
# --------------------------------------------------------------------- #


class TestBackupDatabaseLocally:
    def test_produces_valid_backup_file(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_test_db(src)
        dest_dir = tmp_path / "backups"
        when = datetime(2026, 5, 18, 12, 30, tzinfo=UTC)

        dest = backup_database_locally(src, dest_dir, now=when)

        assert dest.name == "live-20260518-1230.db"
        assert dest.exists()
        # Backup is a valid SQLite file with our test row.
        conn = sqlite3.connect(str(dest))
        try:
            row = conn.execute("SELECT value FROM t").fetchone()
            assert row == ("hello",)
        finally:
            conn.close()

    def test_creates_dest_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_test_db(src)
        dest_dir = tmp_path / "deep" / "backups"
        assert not dest_dir.exists()
        backup_database_locally(src, dest_dir)
        assert dest_dir.exists()

    def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            backup_database_locally(tmp_path / "nope.db", tmp_path)

    def test_does_not_lock_source_for_concurrent_writes(self, tmp_path: Path) -> None:
        """SQLite's online .backup API doesn't block other writers.

        We can't easily prove "doesn't block" in a unit test, but we
        can confirm the source DB is still usable after the backup
        completes."""
        src = tmp_path / "live.db"
        _make_test_db(src)
        backup_database_locally(src, tmp_path / "backups")
        # Source still writable.
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("INSERT INTO t (value) VALUES ('world')")
            conn.commit()
            row_count = conn.execute("SELECT COUNT(*) FROM t").fetchone()
            assert row_count == (2,)
        finally:
            conn.close()


# --------------------------------------------------------------------- #
# prune_old_backups                                                     #
# --------------------------------------------------------------------- #


class TestPruneOldBackups:
    def test_keeps_newest_n(self, tmp_path: Path) -> None:
        # Create 5 backup files; touch mtimes in order.
        files = []
        for i in range(5):
            f = tmp_path / f"live-2026051{i}-0000.db"
            f.write_bytes(b"")
            # Tweak mtime so the newest sort is deterministic
            # (i=4 is newest).
            stamp = time.time() + i
            import os

            os.utime(f, (stamp, stamp))
            files.append(f)

        deleted = prune_old_backups(tmp_path, db_stem="live", keep_n_daily=3)
        assert deleted == 2
        # 3 newest survive
        surviving = sorted(p.name for p in tmp_path.glob("live-*.db"))
        assert surviving == [
            "live-20260512-0000.db",
            "live-20260513-0000.db",
            "live-20260514-0000.db",
        ]

    def test_fewer_files_than_limit_is_noop(self, tmp_path: Path) -> None:
        (tmp_path / "live-20260518-0000.db").write_bytes(b"")
        deleted = prune_old_backups(tmp_path, db_stem="live", keep_n_daily=7)
        assert deleted == 0
        assert (tmp_path / "live-20260518-0000.db").exists()

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no-such-dir"
        deleted = prune_old_backups(nonexistent, db_stem="live", keep_n_daily=7)
        assert deleted == 0

    def test_keep_zero_deletes_everything(self, tmp_path: Path) -> None:
        for i in range(3):
            (tmp_path / f"live-2026051{i}-0000.db").write_bytes(b"")
        deleted = prune_old_backups(tmp_path, db_stem="live", keep_n_daily=0)
        assert deleted == 3
        assert list(tmp_path.glob("live-*.db")) == []

    def test_only_matches_db_stem(self, tmp_path: Path) -> None:
        """Prune scoped to the stem; other DBs' backups are untouched."""
        (tmp_path / "live-20260518-0000.db").write_bytes(b"")
        (tmp_path / "shadow-20260518-0000.db").write_bytes(b"")
        (tmp_path / "harvest-20260518-0000.db").write_bytes(b"")
        deleted = prune_old_backups(tmp_path, db_stem="live", keep_n_daily=0)
        assert deleted == 1
        # Shadow + harvest backups untouched
        assert (tmp_path / "shadow-20260518-0000.db").exists()
        assert (tmp_path / "harvest-20260518-0000.db").exists()

    def test_negative_keep_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be non-negative"):
            prune_old_backups(tmp_path, db_stem="live", keep_n_daily=-1)


# --------------------------------------------------------------------- #
# find_latest_backup                                                    #
# --------------------------------------------------------------------- #


class TestFindLatestBackup:
    def test_returns_newest_file(self, tmp_path: Path) -> None:
        import os

        for i in range(3):
            f = tmp_path / f"live-2026051{i}-0000.db"
            f.write_bytes(b"")
            stamp = time.time() + i
            os.utime(f, (stamp, stamp))
        latest = find_latest_backup(tmp_path, db_stem="live")
        assert latest is not None
        assert latest.name == "live-20260512-0000.db"

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        assert find_latest_backup(tmp_path / "no-such-dir", db_stem="live") is None

    def test_no_matching_files_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "shadow-20260518-0000.db").write_bytes(b"")
        assert find_latest_backup(tmp_path, db_stem="live") is None

    def test_only_matches_db_stem(self, tmp_path: Path) -> None:
        (tmp_path / "live-20260518-0000.db").write_bytes(b"")
        (tmp_path / "harvest-20260518-0000.db").write_bytes(b"")
        latest = find_latest_backup(tmp_path, db_stem="live")
        assert latest is not None
        assert latest.name == "live-20260518-0000.db"


# --------------------------------------------------------------------- #
# verify_backup_restoration (v1.1 restoration smoke test)                #
# --------------------------------------------------------------------- #


class TestVerifyBackupRestoration:
    def test_clean_backup_passes(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_test_db(src)
        backup = backup_database_locally(src, tmp_path / "backups")

        result = verify_backup_restoration(backup)

        assert result.ok is True
        assert result.integrity_check_result == "ok"
        assert result.table_count == 1
        assert result.error is None

    def test_multi_table_backup_checks_every_table(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("CREATE TABLE orders (id INTEGER)")
            conn.execute("CREATE TABLE trades (id INTEGER)")
            conn.execute("INSERT INTO orders VALUES (1)")
            conn.commit()
        finally:
            conn.close()
        backup = backup_database_locally(src, tmp_path / "backups")

        result = verify_backup_restoration(backup)

        assert result.ok is True
        assert result.table_count == 2

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = verify_backup_restoration(tmp_path / "nope.db")
        assert result.ok is False
        assert result.error is not None
        assert "does not exist" in result.error

    def test_non_sqlite_file_fails(self, tmp_path: Path) -> None:
        """A truncated/garbled backup (e.g. a torn write) must fail the
        smoke test rather than reporting false-clean."""
        garbage = tmp_path / "live-20260518-0000.db"
        garbage.write_bytes(b"not a real sqlite file" * 100)

        result = verify_backup_restoration(garbage)

        assert result.ok is False
        assert result.error is not None

    def test_never_touches_source_db(self, tmp_path: Path) -> None:
        """Verification must open only the backup copy -- the source
        DB stays untouched and still writable afterward."""
        src = tmp_path / "live.db"
        _make_test_db(src)
        backup = backup_database_locally(src, tmp_path / "backups")

        verify_backup_restoration(backup)

        conn = sqlite3.connect(str(src))
        try:
            conn.execute("INSERT INTO t (value) VALUES ('still writable')")
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM t").fetchone()
            assert count == (2,)
        finally:
            conn.close()

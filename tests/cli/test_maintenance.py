"""Tests for cli/maintenance daemon (Stage 8.2.D)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import maintenance as cli_maintenance
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.notifier import Notification

pytestmark = pytest.mark.unit


class _RecordingNotifier:
    """Captures every notification it's sent, for assertion."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send_notification(self, notification: Notification) -> None:
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _restore_wobblebot_logger() -> Iterator[None]:
    """Snapshot + restore the ``wobblebot`` logger config per test.

    Same fixture pattern as ``tests/cli/test_web.py`` —
    ``cli_maintenance.main()`` calls ``configure_logging`` which flips
    ``root.propagate = False`` on the ``wobblebot`` subtree.
    """
    root = logging.getLogger("wobblebot")
    snapshot_level = root.level
    snapshot_propagate = root.propagate
    snapshot_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.handlers = snapshot_handlers
        root.propagate = snapshot_propagate
        root.setLevel(snapshot_level)


def _make_sqlite_file(path: Path) -> None:
    """Tiny SQLite file with one row so VACUUM has something to do."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# _vacuum_all                                                           #
# --------------------------------------------------------------------- #


class TestVacuumAll:
    def test_runs_against_all_target_dbs(self, tmp_path: Path) -> None:
        dbs = []
        for name in ("live", "shadow", "operator"):
            p = tmp_path / f"{name}.db"
            _make_sqlite_file(p)
            dbs.append(p)
        ok = cli_maintenance._vacuum_all(dbs)
        assert ok == 3

    def test_missing_db_skipped_others_still_run(self, tmp_path: Path) -> None:
        existing = tmp_path / "live.db"
        _make_sqlite_file(existing)
        missing = tmp_path / "nope.db"
        ok = cli_maintenance._vacuum_all([existing, missing])
        assert ok == 1  # only the existing one


# --------------------------------------------------------------------- #
# _backup_all                                                           #
# --------------------------------------------------------------------- #


class TestBackupAll:
    def test_backs_up_every_target_and_prunes_retention(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        # Pre-seed 5 older backups so retention=2 keeps the new one + 1 old.
        for i in range(5):
            (tmp_path / f"backups").mkdir(exist_ok=True)
            (tmp_path / "backups" / f"live-2026010{i}-0000.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            keep_n_daily_backups=2,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1
        # After the new backup write + retention prune, only 2 files survive.
        surviving = sorted(backup_dir.glob("live-*.db"))
        assert len(surviving) == 2

    def test_missing_db_skipped(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "nope.db")],
            backup_dir=str(tmp_path / "backups"),
            keep_n_daily_backups=7,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 0


# --------------------------------------------------------------------- #
# _verify_all (v1.1 restoration smoke test)                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestVerifyAll:
    async def test_verifies_latest_backup_for_each_target(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))
        # Produce a real backup via the same path _backup_all uses.
        assert cli_maintenance._backup_all(cfg) == 1

        verified = await cli_maintenance._verify_all(cfg, None)

        assert verified == 1

    async def test_skips_db_with_no_backup_yet(self, tmp_path: Path) -> None:
        """A DB added to target_dbs after the last backup cycle has no
        backup file yet -- this must be skipped, not treated as a
        verification failure."""
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(tmp_path / "backups"))

        notifier = _RecordingNotifier()
        verified = await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert verified == 0
        assert notifier.sent == []

    async def test_notifies_on_corrupt_backup(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # A garbled backup file -- simulates a torn/incomplete write.
        (backup_dir / "live-20260101-0000.db").write_bytes(b"not a real sqlite file" * 50)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))

        notifier = _RecordingNotifier()
        verified = await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert verified == 0
        assert len(notifier.sent) == 1
        assert notifier.sent[0].level == "error"
        assert "live" in notifier.sent[0].title

    async def test_none_notifier_does_not_raise_on_failure(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "live-20260101-0000.db").write_bytes(b"garbage" * 50)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))

        verified = await cli_maintenance._verify_all(cfg, None)

        assert verified == 0

    async def test_clean_backup_sends_no_notification(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))
        cli_maintenance._backup_all(cfg)

        notifier = _RecordingNotifier()
        await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert notifier.sent == []


# --------------------------------------------------------------------- #
# _prune_one_cycle                                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPruneCycle:
    async def test_no_source_db_configured_returns_zero(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            prune_source_db=None,
            archive_dir=str(tmp_path / "archive"),
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 0

    async def test_missing_source_db_returns_zero(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            prune_source_db=str(tmp_path / "nope.db"),
            archive_dir=str(tmp_path / "archive"),
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 0

    async def test_archives_and_deletes_old_rows(self, tmp_path: Path) -> None:
        observe_db = tmp_path / "observe.db"
        storage = SQLiteStorageAdapter(str(observe_db))
        await storage.connect()
        # 3 old snapshots (40 days old) + 2 fresh (1 day old).
        for days_ago in (40, 35, 31):
            await storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30000"), currency="USD"),
                Timestamp(dt=datetime.now(UTC) - timedelta(days=days_ago)),
            )
        for days_ago in (1, 0.5):
            await storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30000"), currency="USD"),
                Timestamp(dt=datetime.now(UTC) - timedelta(days=days_ago)),
            )
        await storage.close()

        cfg = MaintenanceConfig(
            target_dbs=[str(observe_db)],
            prune_source_db=str(observe_db),
            archive_dir=str(tmp_path / "archive"),
            prune_price_snapshots_older_than_days=30,
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 3
        # Verify remaining count.
        storage = SQLiteStorageAdapter(str(observe_db))
        await storage.connect()
        try:
            remaining = await storage.get_price_snapshots()
            assert len(remaining) == 2
        finally:
            await storage.close()


# --------------------------------------------------------------------- #
# Retention (ADR-036)                                                   #
# --------------------------------------------------------------------- #


async def _make_schema_db(path: Path) -> None:
    """Real-schema DB file via the adapter (so prunable tables exist)."""
    storage = SQLiteStorageAdapter(str(path))
    await storage.connect()
    await storage.close()


def _insert_old_notification(db_path: Path, *, days_ago: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO notifications (level, title, message, timestamp, context_json, "
            "forwarded, forwarded_at, created_at) VALUES ('info', 't', 'm', ?, '{}', 0, NULL, ?)",
            (stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


class TestResolveRetentionTargets:
    def test_resolves_known_tables(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"news_items": 90, "notifications": 30},
            news_db=str(tmp_path / "news.db"),
            operator_db=str(tmp_path / "op.db"),
        )
        resolved = cli_maintenance._resolve_retention_targets(cfg)
        assert ("news_items", tmp_path / "news.db", 90) in resolved
        assert ("notifications", tmp_path / "op.db", 30) in resolved

    def test_unknown_table_is_boot_error(self, tmp_path: Path) -> None:
        """The forensic ledger can't be named — 'trades' is not in the
        prunable registry, so boot refuses."""
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"trades": 90},
        )
        with pytest.raises(ValueError, match="unknown table"):
            cli_maintenance._resolve_retention_targets(cfg)

    def test_horizon_without_home_db_is_boot_error(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"news_items": 90},
            news_db=None,
        )
        with pytest.raises(ValueError, match="needs maintenance.news_db"):
            cli_maintenance._resolve_retention_targets(cfg)

    def test_empty_retention_resolves_empty(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(target_dbs=[str(tmp_path / "live.db")])
        assert cli_maintenance._resolve_retention_targets(cfg) == []


class TestRetentionPrunes:
    @pytest.mark.asyncio
    async def test_prunes_and_continues_past_failures(self, tmp_path: Path) -> None:
        """One target's DB is missing (logged, skipped); the other
        prunes — fail-soft, same shape as _vacuum_all."""
        op_db = tmp_path / "op.db"
        await _make_schema_db(op_db)
        _insert_old_notification(op_db, days_ago=120.0)
        _insert_old_notification(op_db, days_ago=1.0)
        targets = [
            ("news_items", tmp_path / "missing-news.db", 90),
            ("notifications", op_db, 90),
        ]
        deleted = cli_maintenance._retention_prunes(targets, tmp_path / "archive")
        assert deleted == 1
        archives = list((tmp_path / "archive").glob("notifications-*.csv.gz"))
        assert len(archives) == 1


class TestBackupDedupe:
    def test_fresh_backup_skips_cycle(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        fresh_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (backup_dir / f"live-{fresh_stamp}.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=20.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 0
        assert len(list(backup_dir.glob("live-*.db"))) == 1

    def test_stale_backup_does_not_skip(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # Name-encoded stamp is months old even though mtime is now —
        # the dedupe must read the name, not mtime.
        (backup_dir / "live-20260101-0000.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=20.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1
        assert len(list(backup_dir.glob("live-*.db"))) == 2

    def test_zero_interval_disables_dedupe(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        fresh_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (backup_dir / f"live-{fresh_stamp}.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=0.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1


# --------------------------------------------------------------------- #
# main() pre-async-dispatch paths                                       #
# --------------------------------------------------------------------- #


class TestMain:
    def test_bad_config_path_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = cli_maintenance.main(["--config", str(tmp_path / "nope.yml")])
        assert rc == 2

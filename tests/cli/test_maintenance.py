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

pytestmark = pytest.mark.unit


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
# main() pre-async-dispatch paths                                       #
# --------------------------------------------------------------------- #


class TestMain:
    def test_bad_config_path_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = cli_maintenance.main(["--config", str(tmp_path / "nope.yml")])
        assert rc == 2

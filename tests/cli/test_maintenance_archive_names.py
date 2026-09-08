"""A restarted prune cycle keeps prior archives and exports newly eligible rows."""

import csv
import gzip
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import maintenance
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.domain.value_objects import Price, Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
NOW = datetime(2026, 9, 8, 19, tzinfo=UTC)


class FixedClock(datetime):
    """Reproduce retries even when two invocations share the same exact cutoff."""

    @classmethod
    def now(cls, tz=None):
        return NOW


def archive_rows(directory: Path) -> list[dict[str, str]]:
    rows = []
    for path in directory.glob("*.csv.gz"):
        if path.name.startswith("legacy-"):
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            rows.extend(csv.DictReader(stream))
    return rows


async def test_price_prunes_same_day_and_exact_time_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "datetime", FixedClock)
    database = tmp_path / "observe.db"
    archive = tmp_path / "archive"
    archive.mkdir()
    legacy = archive / "observe-2026-08-09.csv.gz"
    legacy.write_bytes(b"previous archive must remain untouched")
    config = MaintenanceConfig(
        target_dbs=[str(database)],
        prune_source_db=str(database),
        archive_dir=str(archive),
    )
    storage = SQLiteStorageAdapter(str(database))
    await storage.connect()
    try:
        for offset in (40, 41):
            await storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("80000"), currency="USD"),
                Timestamp(dt=NOW - timedelta(days=offset)),
            )
            assert await maintenance._prune_one_cycle(config) == 1
        assert await maintenance._prune_one_cycle(config) == 0
        assert await storage.get_price_snapshots() == []
    finally:
        await storage.close()
    assert legacy.read_bytes() == b"previous archive must remain untouched"
    archives = [path for path in archive.glob("*.csv.gz") if path != legacy]
    assert len(archives) == 2
    contents = []
    for path in archives:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            contents.extend(csv.DictReader(stream))
    assert len(contents) == 2
    assert len({row["observed_at"] for row in contents}) == 2


async def test_retention_retry_after_archive_write_keeps_both_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "datetime", FixedClock)
    database = tmp_path / "news.db"
    storage = SQLiteStorageAdapter(str(database))
    await storage.connect()
    await storage.close()
    # A failing DELETE leaves the completed archive and the row. A retry
    # must make progress with a fresh file, preserving the first export.
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO notifications (level,title,message,timestamp,context_json,created_at) "
            "VALUES ('info','old','fixture',?,'{}',?)",
            ((NOW - timedelta(days=100)).isoformat(),) * 2,
        )
        db.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON notifications "
            "BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END"
        )
        db.commit()
    targets = [("notifications", database, 90)]
    archive = tmp_path / "archive"
    assert maintenance._retention_prunes(targets, archive) == 0
    first = next(archive.glob("*.csv.gz"))
    first_bytes = first.read_bytes()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
        db.execute("DROP TRIGGER block_delete")
        db.commit()
    assert maintenance._retention_prunes(targets, archive) == 1
    assert first.read_bytes() == first_bytes
    assert len(list(archive.glob("*.csv.gz"))) == 2
    assert len(archive_rows(archive)) == 2  # Duplicate recovery export is intentional.
    assert maintenance._retention_prunes(targets, archive) == 0

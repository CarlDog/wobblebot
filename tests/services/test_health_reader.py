"""Observable boundaries of the startup-only health database capability."""

import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wobblebot.services.daemon_health import DaemonStatus
from wobblebot.services.health_reader import HealthDatabaseReader

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _seed(path, *, timestamp=None):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE daemon_heartbeats(name TEXT, last_beat_at TEXT)")
        db.execute(
            "INSERT INTO daemon_heartbeats VALUES (?, ?)",
            ("cli/live", timestamp or datetime.now(UTC).isoformat()),
        )
        db.execute(
            "CREATE TABLE llm_calls(role TEXT, timestamp TEXT, success INTEGER, error_kind TEXT)"
        )
        db.execute(
            "INSERT INTO llm_calls VALUES (?, ?, 1, NULL)",
            ("single", datetime.now(UTC).isoformat()),
        )


def _bind(path):
    return HealthDatabaseReader.bind(observe_db=None, advise_db=None, operator_db=str(path))


def _live(rows):
    return next(row for row in rows if row.name == "cli/live")


async def test_external_custom_path_streaks_and_no_writes(tmp_path):
    path = tmp_path / "operator #100%.db"
    _seed(path)
    before = path.read_bytes()
    reader = _bind(path)
    rows, streaks = await reader.read()
    assert _live(rows).status is DaemonStatus.FRESH
    assert next(row for row in streaks if row.role == "single").has_data
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(TypeError):
        await reader.read(operator_db=tmp_path / "other.db")


async def test_missing_then_first_appearance_pins_once(tmp_path):
    path = tmp_path / "late.db"
    reader = _bind(path)
    rows, streaks = await reader.read()
    assert _live(rows).status is DaemonStatus.UNKNOWN
    assert all(row.unavailable_reason for row in streaks)
    assert not path.exists()
    _seed(path)
    first, second = await asyncio.gather(reader.read(), reader.read())
    assert _live(first[0]).status is DaemonStatus.FRESH
    assert _live(second[0]).status is DaemonStatus.FRESH
    replacement = tmp_path / "replacement.db"
    _seed(replacement)
    os.replace(replacement, path)
    rows, streaks = await reader.read()
    assert _live(rows).status is DaemonStatus.UNKNOWN
    assert all(row.unavailable_reason for row in streaks)
    assert _live((await _bind(path).read())[0]).status is DaemonStatus.FRESH


async def test_existing_file_replacement_requires_restart(tmp_path):
    path = tmp_path / "operator.db"
    _seed(path)
    reader = _bind(path)
    replacement = tmp_path / "replacement.db"
    _seed(replacement)
    os.replace(replacement, path)
    assert _live((await reader.read())[0]).status is DaemonStatus.UNKNOWN
    assert _live((await _bind(path).read())[0]).status is DaemonStatus.FRESH


def _symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {type(exc).__name__}")


async def test_startup_alias_retarget_cannot_redirect_reader(tmp_path):
    original, other, alias = (tmp_path / name for name in ("original.db", "other.db", "alias.db"))
    _seed(original)
    _seed(other, timestamp="2000-01-01T00:00:00+00:00")
    _symlink(alias, original)
    reader = _bind(alias)
    alias.unlink()
    _symlink(alias, other)
    assert _live((await reader.read())[0]).status is DaemonStatus.FRESH


async def test_canonical_file_symlink_replacement_is_rejected(tmp_path):
    path, other = tmp_path / "operator.db", tmp_path / "other.db"
    _seed(path)
    _seed(other)
    reader = _bind(path)
    path.unlink()
    _symlink(path, other)
    assert _live((await reader.read())[0]).status is DaemonStatus.UNKNOWN


@pytest.mark.parametrize("operation", ["resolve", "stat"])
async def test_path_permission_errors_return_unknown(tmp_path, monkeypatch, operation):
    path = tmp_path / "operator.db"
    _seed(path)
    reader = _bind(path)
    original = getattr(Path, operation)

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("denied test fixture")
        return original(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, denied)
        rows, streaks = await reader.read()
    assert _live(rows).status is DaemonStatus.UNKNOWN
    assert all(row.unavailable_reason for row in streaks)
    assert _live((await reader.read())[0]).status is DaemonStatus.FRESH


async def test_directory_is_not_a_database_grant(tmp_path):
    assert _live((await _bind(tmp_path).read())[0]).status is DaemonStatus.UNKNOWN


async def test_wal_updates_and_vacuum_keep_same_grant(tmp_path):
    path = tmp_path / "operator.db"
    _seed(path, timestamp="2000-01-01T00:00:00+00:00")
    with closing(sqlite3.connect(path)) as writer, writer:
        writer.execute("PRAGMA journal_mode=WAL")
        reader = _bind(path)
        assert _live((await reader.read())[0]).status is DaemonStatus.STALE
        writer.execute(
            "UPDATE daemon_heartbeats SET last_beat_at=?", (datetime.now(UTC).isoformat(),)
        )
        writer.commit()
        assert Path(str(path) + "-wal").exists()
        assert _live((await reader.read())[0]).status is DaemonStatus.FRESH
        writer.execute("VACUUM")
        assert _live((await reader.read())[0]).status is DaemonStatus.FRESH


async def test_mid_read_replacement_discards_both_observations(tmp_path, monkeypatch):
    from wobblebot.services import health_reader

    path = tmp_path / "operator.db"
    _seed(path)
    reader = _bind(path)
    original = health_reader.fetch_llm_call_streaks

    async def replace_after_read(**kwargs):
        result = await original(**kwargs)
        if kwargs["operator_db"] is not None:
            replacement = tmp_path / "replacement.db"
            _seed(replacement)
            os.replace(replacement, path)
        return result

    monkeypatch.setattr(health_reader, "fetch_llm_call_streaks", replace_after_read)
    rows, streaks = await reader.read()
    assert _live(rows).status is DaemonStatus.UNKNOWN
    assert all(row.unavailable_reason for row in streaks)

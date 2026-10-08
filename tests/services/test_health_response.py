"""Independent observer: real SQLite transitions, restarts and delivery effects."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.delivery import forward_notifications
from wobblebot.services.health_response import HealthObserver

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
EXAMPLE = Path(__file__).resolve().parents[2] / "config/settings.example.yml"


def operator_config(path):
    config = load_resolved_config(EXAMPLE, "cpu-only")
    return config.model_copy(
        update={
            "observe": None,
            "advise": None,
            "news": None,
            "live": None,
            "harvest": None,
            "maintenance": None,
            "operator": config.operator.model_copy(update={"operator_db": str(path)}),
            "delivery": config.delivery.model_copy(
                update={
                    "operator_db": str(path),
                    "observe_db": None,
                    "advise_db": None,
                }
            ),
        }
    )


async def test_dead_operator_alert_survives_observer_restart_then_recovers(tmp_path):
    path = tmp_path / "operator #1.db"
    config = operator_config(path)
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    now = datetime.now(UTC)
    try:
        await storage.upsert_daemon_heartbeat("cli/operator", now - timedelta(hours=1))
        assert await HealthObserver(config, storage).poll(now=now) == 1
        assert await HealthObserver(config, storage).poll(now=now) == 0
        transport = AsyncMock()
        transport.send_embed.return_value = "fixture-message"
        assert await forward_notifications(storage, transport, "123") == 1
        (row,) = await storage.get_notifications()
        assert (
            row.notification.level == "critical"
            and "stale" in row.notification.title
            and row.forwarded
        )
        await storage.upsert_daemon_heartbeat("cli/operator", now)
        assert await HealthObserver(config, storage).poll(now=now) == 1
        rows = await storage.get_notifications()
        assert any(
            row.notification.level == "info" and "fresh" in row.notification.title for row in rows
        )
    finally:
        await storage.close()


async def test_unknown_grace_and_competing_observers_queue_one_alert(tmp_path):
    path = tmp_path / "operator.db"
    config = operator_config(path)
    storage = SQLiteStorageAdapter(path)
    other = SQLiteStorageAdapter(path)
    await storage.connect()
    await other.connect()
    now = datetime.now(UTC)
    try:
        assert await HealthObserver(config, storage, started_at=now).poll(now=now) == 0
        start = now - timedelta(hours=1)
        counts = await asyncio.gather(
            HealthObserver(config, storage, started_at=start).poll(now=now),
            HealthObserver(config, other, started_at=start).poll(now=now),
        )
        assert sorted(counts) == [0, 1]
        (row,) = await storage.get_notifications()
        assert "unknown" in row.notification.title
    finally:
        await storage.close()
        await other.close()


async def test_alert_enqueue_failure_does_not_consume_transition(tmp_path):
    path = tmp_path / "operator.db"
    config = operator_config(path)
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    now = datetime.now(UTC)
    try:
        await storage.upsert_daemon_heartbeat("cli/operator", now - timedelta(hours=1))
        conn = storage._require_conn()
        await conn.execute(
            "CREATE TRIGGER fail_alert BEFORE INSERT ON notifications "
            "BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
        await conn.commit()
        with pytest.raises(StorageError):
            await HealthObserver(config, storage).poll(now=now)
        await conn.execute("DROP TRIGGER fail_alert")
        await conn.commit()
        assert await HealthObserver(config, storage).poll(now=now) == 1
    finally:
        await storage.close()

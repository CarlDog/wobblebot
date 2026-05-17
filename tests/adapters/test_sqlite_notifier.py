"""Tests for SqliteNotifierAdapter (Stage 5.5.A).

The adapter is thin — its job is to convert NotifierPort calls into
StoragePort.save_notification calls. Tests verify the conversion plus
the error wrapping (StorageError → NotifierError).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NotifierError, StorageError
from wobblebot.ports.notifier import Notification

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _notification() -> Notification:
    return Notification(
        level="info",
        title="hello",
        message="world",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        context={"k": "v"},
    )


async def test_send_notification_persists_row(storage: SQLiteStorageAdapter) -> None:
    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_notification(_notification())
    rows = await storage.get_notifications()
    assert len(rows) == 1
    assert rows[0].notification.title == "hello"
    assert rows[0].notification.context == {"k": "v"}


async def test_send_error_alert_synthesizes_critical_notification(
    storage: SQLiteStorageAdapter,
) -> None:
    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_error_alert(
        ValueError("something broke"), {"tick": 42, "symbol": "BTC/USD"}
    )
    rows = await storage.get_notifications()
    assert len(rows) == 1
    n = rows[0].notification
    assert n.level == "critical"
    assert "ValueError" in n.title
    assert n.message == "something broke"
    assert n.context == {"tick": 42, "symbol": "BTC/USD"}


async def test_storage_error_wraps_as_notifier_error() -> None:
    # Use a deliberately broken storage that raises on save.
    class _BrokenStorage:
        async def save_notification(self, _: Notification) -> int:
            raise StorageError("disk gone")

    notifier = SqliteNotifierAdapter(_BrokenStorage())  # type: ignore[arg-type]
    with pytest.raises(NotifierError, match="Failed to persist notification"):
        await notifier.send_notification(_notification())


async def test_send_error_alert_with_empty_message(storage: SQLiteStorageAdapter) -> None:
    notifier = SqliteNotifierAdapter(storage)

    class _EmptyError(Exception):
        pass

    await notifier.send_error_alert(_EmptyError(), {})
    rows = await storage.get_notifications()
    assert len(rows) == 1
    # Falls back to repr() when str(error) is empty
    assert rows[0].notification.message

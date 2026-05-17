"""SQLiteStorageAdapter tests for the notifications persistence (Stage 5.5.A)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notifier import Notification

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _notification(
    *,
    level: str = "info",
    title: str = "test",
    message: str = "hello",
    context: dict[str, object] | None = None,
    offset_seconds: int = 0,
) -> Notification:
    return Notification(
        level=level,  # type: ignore[arg-type]
        title=title,
        message=message,
        timestamp=Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds)),
        context=context or {},
    )


# --------------------------------------------------------------------- #
# Save + round-trip                                                     #
# --------------------------------------------------------------------- #


async def test_save_then_read(storage: SQLiteStorageAdapter) -> None:
    row_id = await storage.save_notification(_notification())
    assert isinstance(row_id, int) and row_id > 0
    rows = await storage.get_notifications()
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].notification.title == "test"
    assert rows[0].forwarded is False
    assert rows[0].forwarded_at is None


async def test_context_dict_round_trips(storage: SQLiteStorageAdapter) -> None:
    n = _notification(context={"symbol": "BTC/USD", "count": 5, "nested": {"k": "v"}})
    await storage.save_notification(n)
    rows = await storage.get_notifications()
    assert rows[0].notification.context == {
        "symbol": "BTC/USD",
        "count": 5,
        "nested": {"k": "v"},
    }


async def test_each_level_persists_correctly(storage: SQLiteStorageAdapter) -> None:
    for level in ("info", "warning", "error", "critical"):
        await storage.save_notification(_notification(level=level))
    rows = await storage.get_notifications()
    levels = {r.notification.level for r in rows}
    assert levels == {"info", "warning", "error", "critical"}


# --------------------------------------------------------------------- #
# Forwarded filter + mark-forwarded                                     #
# --------------------------------------------------------------------- #


async def test_get_notifications_filters_by_forwarded(storage: SQLiteStorageAdapter) -> None:
    a_id = await storage.save_notification(_notification(title="A"))
    b_id = await storage.save_notification(_notification(title="B"))
    await storage.mark_notification_forwarded(a_id, Timestamp(dt=datetime.now(UTC)))
    unforwarded = await storage.get_notifications(forwarded=False)
    forwarded = await storage.get_notifications(forwarded=True)
    assert [r.id for r in unforwarded] == [b_id]
    assert [r.id for r in forwarded] == [a_id]


async def test_mark_forwarded_sets_timestamp(storage: SQLiteStorageAdapter) -> None:
    row_id = await storage.save_notification(_notification())
    ts = Timestamp(dt=datetime.now(UTC))
    await storage.mark_notification_forwarded(row_id, ts)
    rows = await storage.get_notifications(forwarded=True)
    assert len(rows) == 1
    assert rows[0].forwarded is True
    assert rows[0].forwarded_at is not None
    assert rows[0].forwarded_at.dt == ts.dt


async def test_mark_forwarded_unknown_id_raises(storage: SQLiteStorageAdapter) -> None:
    with pytest.raises(StorageError, match="not found"):
        await storage.mark_notification_forwarded(99999, Timestamp(dt=datetime.now(UTC)))


async def test_mark_forwarded_idempotent_re_mark_updates_timestamp(
    storage: SQLiteStorageAdapter,
) -> None:
    row_id = await storage.save_notification(_notification())
    ts1 = Timestamp(dt=datetime.now(UTC))
    ts2 = Timestamp(dt=datetime.now(UTC) + timedelta(seconds=10))
    await storage.mark_notification_forwarded(row_id, ts1)
    await storage.mark_notification_forwarded(row_id, ts2)
    rows = await storage.get_notifications(forwarded=True)
    assert len(rows) == 1
    assert rows[0].forwarded_at is not None
    assert rows[0].forwarded_at.dt == ts2.dt


# --------------------------------------------------------------------- #
# Ordering + limit                                                      #
# --------------------------------------------------------------------- #


async def test_get_notifications_orders_by_created_at_asc(
    storage: SQLiteStorageAdapter,
) -> None:
    a_id = await storage.save_notification(_notification(title="A"))
    b_id = await storage.save_notification(_notification(title="B"))
    c_id = await storage.save_notification(_notification(title="C"))
    rows = await storage.get_notifications()
    assert [r.id for r in rows] == [a_id, b_id, c_id]


async def test_get_notifications_respects_limit(storage: SQLiteStorageAdapter) -> None:
    for _ in range(5):
        await storage.save_notification(_notification())
    rows = await storage.get_notifications(limit=2)
    assert len(rows) == 2


# --------------------------------------------------------------------- #
# Schema-level guarantees                                               #
# --------------------------------------------------------------------- #


async def test_level_check_rejects_unknown_value(
    storage: SQLiteStorageAdapter,
) -> None:
    # Pydantic blocks invalid Notification construction; this asserts
    # the CHECK constraint catches a direct-SQL bypass.
    conn = storage._require_conn()  # pylint: disable=protected-access
    with pytest.raises(Exception):  # IntegrityError
        await conn.execute(
            """
            INSERT INTO notifications (level, title, message, timestamp, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("spicy", "x", "x", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        await conn.commit()

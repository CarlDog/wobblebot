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


# --------------------------------------------------------------------- #
# Typed-event round-trip (P3 renderers slice)                            #
# --------------------------------------------------------------------- #


async def test_typed_event_round_trips_through_context_json(
    storage: SQLiteStorageAdapter,
) -> None:
    """An event serializes into the existing context_json column and
    reconstructs as the SAME typed variant — no schema migration."""
    from wobblebot.ports.notification_events import FillEvent

    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_notification(
        Notification(
            level="info",
            title="Fills: BTC/USD (2)",
            message="2 order(s) filled",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            event=FillEvent(symbol="BTC/USD", fills=2, counters_placed=2, tick=17),
        )
    )
    rows = await storage.get_notifications()
    assert len(rows) == 1
    event = rows[0].notification.event
    assert isinstance(event, FillEvent)
    assert event.symbol == "BTC/USD"
    assert event.fills == 2
    assert event.tick == 17
    # Generic consumers (web /notifications, /history) still see the
    # data: the raw event dict doubles as the context.
    assert rows[0].notification.context["kind"] == "fill"


async def test_decimal_fields_round_trip_exactly(storage: SQLiteStorageAdapter) -> None:
    from decimal import Decimal

    from wobblebot.ports.notification_events import LossCapEvent

    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_notification(
        Notification(
            level="error",
            title="Loss cap tripped",
            message="cap",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            event=LossCapEvent(
                session_pnl_usd=Decimal("-5.1234567890123456789"),
                limit_usd=Decimal("5.00"),
                tick=3,
            ),
        )
    )
    rows = await storage.get_notifications()
    event = rows[0].notification.event
    assert isinstance(event, LossCapEvent)
    assert event.session_pnl_usd == Decimal("-5.1234567890123456789")


async def test_legacy_context_row_yields_event_none(storage: SQLiteStorageAdapter) -> None:
    """Pre-slice rows (plain context dict, no 'kind') keep working —
    event=None routes them down the legacy render path."""
    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_notification(_notification())
    rows = await storage.get_notifications()
    assert rows[0].notification.event is None
    assert rows[0].notification.context == {"k": "v"}


async def test_unknown_kind_degrades_to_legacy(storage: SQLiteStorageAdapter) -> None:
    """A context dict that HAPPENS to carry an unrecognized 'kind' key
    (or a future event this build doesn't know) must not poison the
    read — event=None, context preserved."""
    notifier = SqliteNotifierAdapter(storage)
    await notifier.send_notification(
        Notification(
            level="info",
            title="t",
            message="m",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            context={"kind": "from_the_future", "x": 1},
        )
    )
    rows = await storage.get_notifications()
    assert rows[0].notification.event is None
    assert rows[0].notification.context == {"kind": "from_the_future", "x": 1}

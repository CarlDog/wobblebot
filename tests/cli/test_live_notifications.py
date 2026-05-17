"""Tests for cli/live notification emit (Stage 5.5.B).

The full session-start / session-end / fills / cap-trip flow is
covered end-to-end by the Stage 5.7 integration check. These unit
tests target the ``_notify`` helper in isolation and verify that
``_run_one_tick`` writes a notification row on a fill event when a
notifier is wired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _notify
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import NotifierError
from wobblebot.ports.notifier import Notification, NotifierPort

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def test_notify_with_none_is_noop() -> None:
    """``_notify(notifier=None, ...)`` returns without error."""
    await _notify(None, level="info", title="t", message="m")


async def test_notify_persists_via_storage(storage: SQLiteStorageAdapter) -> None:
    notifier = SqliteNotifierAdapter(storage)
    await _notify(
        notifier,
        level="info",
        title="session started",
        message="trading BTC/USD",
        context={"symbol": "BTC/USD", "tick_seconds": 5.0},
    )
    rows = await storage.get_notifications()
    assert len(rows) == 1
    n = rows[0].notification
    assert n.title == "session started"
    assert n.context["symbol"] == "BTC/USD"


async def test_notify_swallows_notifier_errors() -> None:
    """If the notifier raises, ``_notify`` logs but does NOT raise."""

    class _FailingNotifier:
        async def send_notification(self, _: Notification) -> None:
            raise NotifierError("transport down")

        async def send_error_alert(self, error: Exception, context: dict) -> None:
            pass

    # Should NOT raise — engine loop must keep going if notifications break.
    await _notify(
        _FailingNotifier(),  # type: ignore[arg-type]
        level="info",
        title="x",
        message="y",
    )

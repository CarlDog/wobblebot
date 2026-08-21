"""Regression tests for the 2026-08-20 incident (bug 3): a book-vanish
hold pages exactly once by design (see ``held_book_vanish``'s ADR-037
anti-spam comment in cli/live) but production twice went many hours
with symbols HELD and only that one initial alert to notice by (ADA
~13h on 2026-08-19; BTC/SOL/ETH/ADA/XRP ~18h+ on 2026-08-20). These pin
``_check_held_reminder``: a single aggregate reminder fires on a
cadence while ANY symbol remains held, and stays quiet otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import live as live_module
from wobblebot.cli.live import _check_held_reminder
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


class _FakeEngine:
    """Duck-typed stand-in exposing only what ``_check_held_reminder``
    calls (``hold_reason``) -- avoids standing up a real GridEngine +
    exchange + book-vanish sequence for a check that only reads
    already-computed hold state."""

    def __init__(self, reasons: dict[Symbol, str]) -> None:
        self._reasons = reasons

    def hold_reason(self, symbol: Symbol) -> str | None:
        return self._reasons.get(symbol)


def _live(**overrides: object) -> LiveConfig:
    overrides.setdefault("symbols", [BTC_USD])
    return LiveConfig(**overrides)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def test_no_held_symbols_stays_quiet_and_returns_none(
    storage: SQLiteStorageAdapter,
) -> None:
    engine = _FakeEngine({})
    notifier = SqliteNotifierAdapter(storage)

    result = await _check_held_reminder(engine, _live(), notifier, tick=1, last_reminder_at=123.0)

    assert result is None
    assert await storage.get_notifications() == []


async def test_first_held_tick_starts_the_clock_without_notifying(
    storage: SQLiteStorageAdapter,
) -> None:
    engine = _FakeEngine({BTC_USD: "book_vanish"})
    notifier = SqliteNotifierAdapter(storage)

    result = await _check_held_reminder(engine, _live(), notifier, tick=1, last_reminder_at=None)

    assert result is not None  # clock started, not a notify
    assert await storage.get_notifications() == []


async def test_reminder_fires_once_interval_elapses(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeEngine({BTC_USD: "book_vanish"})
    notifier = SqliteNotifierAdapter(storage)
    live = _live(held_reminder_seconds=100.0)
    monkeypatch.setattr(live_module.time, "monotonic", lambda: 1000.0)

    result = await _check_held_reminder(engine, live, notifier, tick=5, last_reminder_at=900.0)

    assert result == 1000.0
    rows = await storage.get_notifications()
    assert len(rows) == 1
    assert rows[0].notification.level == "warning"
    assert "1 symbol(s)" in rows[0].notification.message
    assert "BTC/USD" in rows[0].notification.message
    assert rows[0].notification.context["symbols"] == ["BTC/USD"]


async def test_reminder_does_not_fire_before_interval(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeEngine({BTC_USD: "book_vanish"})
    notifier = SqliteNotifierAdapter(storage)
    live = _live(held_reminder_seconds=100.0)
    monkeypatch.setattr(live_module.time, "monotonic", lambda: 950.0)

    result = await _check_held_reminder(engine, live, notifier, tick=5, last_reminder_at=900.0)

    assert result == 900.0  # unchanged -- not due yet
    assert await storage.get_notifications() == []


async def test_resuming_clears_the_clock_for_the_next_episode(
    storage: SQLiteStorageAdapter,
) -> None:
    """Once every held symbol resumes, the tracker resets so a FUTURE
    hold episode gets a full window before its first reminder rather
    than inheriting a stale start time."""
    engine = _FakeEngine({})  # nothing held anymore
    notifier = SqliteNotifierAdapter(storage)

    result = await _check_held_reminder(engine, _live(), notifier, tick=9, last_reminder_at=500.0)

    assert result is None


async def test_held_reminder_seconds_none_disables_it(storage: SQLiteStorageAdapter) -> None:
    engine = _FakeEngine({BTC_USD: "book_vanish"})
    notifier = SqliteNotifierAdapter(storage)
    live = _live(held_reminder_seconds=None)

    result = await _check_held_reminder(engine, live, notifier, tick=1, last_reminder_at=0.0)

    assert result is None
    assert await storage.get_notifications() == []


async def test_multiple_held_symbols_get_one_aggregate_notification(
    storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the 2026-08-20 incident shape: 5 symbols held at once.
    The reminder must stay a SINGLE notification, not one per symbol --
    that would recreate the spam problem the one-time page avoids."""
    engine = _FakeEngine({BTC_USD: "book_vanish", ETH_USD: "book_vanish"})
    notifier = SqliteNotifierAdapter(storage)
    live = _live(symbols=[BTC_USD, ETH_USD], held_reminder_seconds=100.0)
    monkeypatch.setattr(live_module.time, "monotonic", lambda: 1000.0)

    await _check_held_reminder(engine, live, notifier, tick=1, last_reminder_at=800.0)

    rows = await storage.get_notifications()
    assert len(rows) == 1
    assert "2 symbol(s)" in rows[0].notification.message
    assert "BTC/USD" in rows[0].notification.message
    assert "ETH/USD" in rows[0].notification.message


async def test_plain_operator_pause_is_not_treated_as_held(
    storage: SQLiteStorageAdapter,
) -> None:
    """``hold_reason`` returns None for a plain operator pause (only
    book_vanish sets a reason) -- the reminder must not fire for those."""
    engine = _FakeEngine({})  # operator pause: no hold_reason entry
    notifier = SqliteNotifierAdapter(storage)

    result = await _check_held_reminder(engine, _live(), notifier, tick=1, last_reminder_at=None)

    assert result is None
    assert await storage.get_notifications() == []

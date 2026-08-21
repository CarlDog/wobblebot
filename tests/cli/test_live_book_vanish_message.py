"""Regression tests for the 2026-08-20 incident (bug 2): the "Book
vanished" notification reads identically alarming whether the cause was
Kraken's own dead-man's-switch firing during a self-resolving API
outage (what actually happened — the safety net worked as designed) or
a genuinely unexplained external cancel (which really would warrant
"investigate before resuming"). The message now cites
``escalation.dms_failure_streak`` — still live at the moment of
detection, since the DMS ping runs earlier in the same tick — to pick
the honest framing. The symbol still HOLDs either way; only the
message's certainty changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _AuthEscalation, _run_one_tick
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )


def _live() -> LiveConfig:
    return LiveConfig(symbols=[BTC_USD])


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _vanish_the_book(exchange: MockExchangeAdapter, engine: GridEngine, storage) -> None:
    """Lay the initial grid, then cancel every order on the EXCHANGE
    ONLY — storage still believes they're open. This is exactly what a
    DMS purge (or a manual UI cancel) looks like to the next tick, per
    ``TestBookVanishHold`` in ``tests/services/test_grid_engine.py``."""
    await engine.step(BTC_USD)
    for order in await storage.get_open_orders(symbol=BTC_USD):
        exchange.inject_partial_cancel(order, filled_amount=Decimal("0"))


async def _vanish_notification(storage: SQLiteStorageAdapter):
    rows = await storage.get_notifications()
    matches = [r for r in rows if r.notification.title.startswith("Book vanished")]
    assert len(matches) == 1
    return matches[0].notification


async def test_message_cites_dms_when_streak_was_active(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()
    escalation.dms_failure_streak = 7  # DMS was actively unhealthy this tick

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    message = (await _vanish_notification(storage)).message
    assert "dead-man's-switch reset was failing (7 consecutive failures)" in message
    assert "not an external action" in message
    assert "Resume when ready" in message
    assert "Investigate before resuming" not in message
    context = (await _vanish_notification(storage)).context
    assert context["dms_failure_streak"] == 7


async def test_message_stays_sharp_when_dms_was_healthy(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()  # streak == 0 -- DMS was healthy right up to the vanish

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    message = (await _vanish_notification(storage)).message
    assert "Investigate before resuming" in message
    assert "dead-man's-switch reset was failing" not in message
    context = (await _vanish_notification(storage)).context
    assert context["dms_failure_streak"] == 0


async def test_message_stays_sharp_when_no_escalation_wired(storage: SQLiteStorageAdapter) -> None:
    """Callers that don't wire ``_AuthEscalation`` at all (escalation=None)
    must fall back to the original sharper framing, not crash."""
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    await _run_one_tick(exchange, engine, _live(), 1, Decimal("100000"), notifier)

    message = (await _vanish_notification(storage)).message
    assert "Investigate before resuming" in message


async def test_both_variants_still_hold_and_page_critical(storage: SQLiteStorageAdapter) -> None:
    """Only the message's certainty changes -- severity and the HOLD
    itself must not."""
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()
    escalation.dms_failure_streak = 3

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    notification = await _vanish_notification(storage)
    assert notification.level == "critical"
    assert engine.is_paused(BTC_USD)
    assert engine.hold_reason(BTC_USD) == "book_vanish"

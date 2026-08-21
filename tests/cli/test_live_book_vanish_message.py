"""Regression tests for the 2026-08-20 incident (bug 2): the "Book
vanished" notification reads identically alarming whether the cause was
Kraken's own dead-man's-switch firing during a self-resolving API
outage (what actually happened — the safety net worked as designed) or
a genuinely unexplained external cancel (which really would warrant
"investigate before resuming").

The message's framing predicate is ``escalation.dms_timer_expired_this_tick``
— whether Kraken's own PROMISED auto-cancel deadline (``dms_trigger_at``,
from the last confirmed DMS ping) had already passed as of the start of
this tick — NOT a raw failure-streak count. A code-review pass on the
first version of this fix (which used ``dms_failure_streak > 0``) caught
two real gaps a streak count can't cover, both pinned here:

1. A short streak (1-2, even the alert threshold of 3) at the default
   5s tick is only ~15s -- far short of a 60s timeout -- so "streak > 0"
   would falsely blame the timer for a same-window external cancel that
   had nothing to do with it.
2. A same-tick DMS recovery (the ping succeeds right before the vanish
   is detected, same loop iteration) would reset the streak to 0 via
   ``note_dms_success()`` BEFORE ``_run_one_tick`` runs, hiding the
   failure episode that just ended and mislabeling a genuinely
   DMS-caused vanish as suspicious.

The symbol still HOLDs either way in every scenario below; only the
message's certainty changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _AuthEscalation, _run_loop, _run_one_tick
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


async def test_message_cites_dms_when_timer_had_expired(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()
    escalation.dms_failure_streak = 7
    escalation.dms_timer_expired_this_tick = True  # Kraken's promised deadline had passed

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    notification = await _vanish_notification(storage)
    message = notification.message
    assert "dead-man's-switch reset was failing (7 consecutive failures)" in message
    assert "not an external action" in message
    assert "Resume when ready" in message
    assert "Investigate before resuming" not in message
    assert notification.context["dms_failure_streak"] == 7
    assert notification.context["dms_timer_expired"] is True


async def test_message_stays_sharp_when_dms_was_healthy(storage: SQLiteStorageAdapter) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()  # no failures at all -- DMS was healthy

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    notification = await _vanish_notification(storage)
    assert "Investigate before resuming" in notification.message
    assert "dead-man's-switch reset was failing" not in notification.message
    assert notification.context["dms_failure_streak"] == 0
    assert notification.context["dms_timer_expired"] is False


async def test_short_streak_alone_does_not_earn_the_reassuring_framing(
    storage: SQLiteStorageAdapter,
) -> None:
    """Code-review regression: a streak of 3 (the OLD trigger condition,
    and the streak-ALERT threshold) is only ~15s at the default 5s tick
    -- nowhere near a real 60s DMS timeout. Without genuine deadline
    evidence, this must NOT get the reassuring "most likely Kraken's own
    timer" framing, because the timer could not plausibly have lapsed
    yet -- a same-window external cancel is just as likely."""
    exchange = _exchange()
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    notifier = SqliteNotifierAdapter(storage)
    await _vanish_the_book(exchange, engine, storage)

    escalation = _AuthEscalation()
    escalation.dms_failure_streak = 3  # at the alert threshold...
    escalation.dms_timer_expired_this_tick = False  # ...but no deadline evidence

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    notification = await _vanish_notification(storage)
    assert "Investigate before resuming" in notification.message
    assert "dead-man's-switch reset was failing" not in notification.message


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
    escalation.dms_timer_expired_this_tick = True

    await _run_one_tick(
        exchange, engine, _live(), 1, Decimal("100000"), notifier, escalation=escalation
    )

    notification = await _vanish_notification(storage)
    assert notification.level == "critical"
    assert engine.is_paused(BTC_USD)
    assert engine.hold_reason(BTC_USD) == "book_vanish"


class _LateVanishExchange(MockExchangeAdapter):
    """Drives the exact same-tick-recovery timeline through the REAL
    ``_run_loop`` wiring, not just a manually-set flag:

    - Tick 1's DMS ping SUCCEEDS but reports an already-PAST deadline
      (as if from severe clock/latency skew) -- ``dms_failure_streak``
      stays 0 the entire test; only the deadline is stale.
    - Tick 2's ``get_open_orders`` (called inside ``_run_one_tick``)
      externally cancels the resting orders on its way out, so THIS
      tick discovers the vanish.
    - Tick 2's own DMS ping (called BEFORE ``_run_one_tick`` in the same
      iteration) succeeds normally, updating the deadline back into the
      future -- exactly the "recovery tick" scenario: by the time the
      vanish message is built, note_dms_success() has already run.

    If the snapshot were taken AFTER the ping (or not snapshotted at
    all), this vanish would incorrectly read as DMS-healthy.
    """

    def __init__(self, *args: object, stop_after: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dms_call_count = 0
        self.open_orders_call_count = 0
        self._stop_after = stop_after
        self.engine: GridEngine | None = None

    async def set_dead_mans_switch(self, timeout_seconds: int):  # type: ignore[no-untyped-def]
        self.dms_call_count += 1
        if self.dms_call_count >= self._stop_after and self.engine is not None:
            self.engine.request_stop()
        if timeout_seconds == 0:
            return await super().set_dead_mans_switch(timeout_seconds)
        if self.dms_call_count == 1:
            self.last_dead_mans_switch_seconds = timeout_seconds
            return datetime.now(UTC) - timedelta(seconds=1)  # already-past, from tick 1
        return await super().set_dead_mans_switch(timeout_seconds)

    async def get_open_orders(self, symbol: Symbol | None = None):  # type: ignore[no-untyped-def]
        self.open_orders_call_count += 1
        if self.open_orders_call_count == 2:
            for order in list(self._open_orders.values()):
                self.inject_partial_cancel(order, filled_amount=Decimal("0"))
        return await super().get_open_orders(symbol)


async def test_same_tick_dms_recovery_still_gets_the_reassuring_framing(
    storage: SQLiteStorageAdapter,
) -> None:
    """Wiring-level regression for the exact review comment this fix
    addresses: a DMS recovery on the SAME tick a vanish is discovered
    must not erase the evidence that the deadline had already passed."""
    exch = _LateVanishExchange(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
        stop_after=3,
    )
    engine = GridEngine(exch, storage, grid_config(), safety_config())
    exch.engine = engine
    notifier = SqliteNotifierAdapter(storage)
    live = LiveConfig(symbols=[BTC_USD], tick_seconds=0.001)

    await _run_loop(exch, engine, live, storage, asyncio.Event(), notifier=notifier)

    notification = await _vanish_notification(storage)
    assert "not an external action" in notification.message
    assert "Investigate before resuming" not in notification.message
    assert notification.context["dms_timer_expired"] is True
    # The point of the fix: this held despite the streak never having
    # risen at all -- pure count-based evidence would have missed it.
    assert notification.context["dms_failure_streak"] == 0

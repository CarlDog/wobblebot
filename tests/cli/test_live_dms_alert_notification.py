"""Regression test for the 2026-08-20 incident (bug 1): a sustained
``CancelAllOrdersAfter`` (DMS reset) failure streak, interleaved with an
UNRELATED private call (``get_open_orders``, polled every tick) that
kept succeeding, must still page the "Dead-man's-switch resets failing"
critical. Pre-fix, ``_AuthEscalation.note_success()`` was called from
BOTH the DMS-ping success path AND the generic per-tick OpenOrders
success path, so the unrelated OpenOrders successes wiped
``dms_failure_streak`` back to 0 every tick and the alert never fired
despite ~40 consecutive real-world DMS failures.

This drives the actual ``_run_loop`` (not just the ``_AuthEscalation``
unit in isolation) so a regression in the CALL SITES — e.g. reverting
``note_dms_success()`` back to the shared ``note_success()`` — is
caught even if the class logic itself stays correct.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _DMS_FAILURE_STREAK_ALERT, _run_loop
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


class _DmsAlwaysFailingExchange(MockExchangeAdapter):
    """DMS reset (``CancelAllOrdersAfter``) fails every call — mirrors
    the incident's ~6-minute partial Kraken outage — while every OTHER
    private call (OpenOrders, TradesHistory, ...) keeps succeeding
    normally via the inherited MockExchangeAdapter behavior."""

    def __init__(self, *args: object, stop_after: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dms_call_count = 0
        self._stop_after = stop_after
        self.engine: GridEngine | None = None

    async def set_dead_mans_switch(self, timeout_seconds: int):  # type: ignore[no-untyped-def]
        self.dms_call_count += 1
        if self.dms_call_count >= self._stop_after and self.engine is not None:
            self.engine.request_stop()
        raise ExchangeError("simulated partial outage", codes=["EService:Unavailable"])


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _live(**overrides: object) -> LiveConfig:
    overrides.setdefault("symbols", [BTC_USD])
    overrides.setdefault("tick_seconds", 0.001)
    return LiveConfig(**overrides)  # type: ignore[arg-type]


async def test_sustained_dms_failures_page_despite_healthy_open_orders(
    storage: SQLiteStorageAdapter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _DMS_FAILURE_STREAK_ALERT == 3, "test assumes the real threshold"
    exch = _DmsAlwaysFailingExchange(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
        stop_after=5,
    )
    engine = GridEngine(exch, storage, grid_config(), safety_config())
    exch.engine = engine
    notifier = SqliteNotifierAdapter(storage)

    with caplog.at_level(logging.WARNING, logger="wobblebot.cli.live"):
        await _run_loop(exch, engine, _live(), storage, asyncio.Event(), notifier=notifier)

    # At least 5 consecutive DMS-ping failures happened (past the alert
    # threshold of 3), each followed in the SAME tick by a successful
    # OpenOrders fetch inside _run_one_tick -- exactly the interleaving
    # that hid the bug pre-fix. (One further disarm attempt happens in
    # the shutdown `finally` block, also failing, hence >= not ==.)
    assert exch.dms_call_count >= 5

    rows = await storage.get_notifications()
    alerts = [r for r in rows if r.notification.title == "Dead-man's-switch resets failing"]
    assert len(alerts) == 1, (
        "the DMS-failure alert must fire once the streak crosses the threshold, "
        "even though an unrelated private call kept succeeding every tick"
    )
    assert alerts[0].notification.level == "critical"
    assert str(_DMS_FAILURE_STREAK_ALERT) in alerts[0].notification.message
    # 2026-09-03 follow-up: the alert carries the last CONFIRMED deadline.
    # This exchange never confirmed an arm (every ping raised), so it is
    # honestly None rather than absent.
    assert "last_confirmed_trigger_at" in alerts[0].notification.context
    assert alerts[0].notification.context["last_confirmed_trigger_at"] is None


class _DmsArmsOnceThenFailsExchange(MockExchangeAdapter):
    """Confirms the DMS arm once (returning a real ``triggerTime``), then fails
    every subsequent reset — the 2026-09-03 shape, where the switch WAS armed
    before the stall. Exercises the non-None ``last_confirmed_trigger_at``
    branch that the original assertion left as dead code under test."""

    def __init__(self, *args: object, stop_after: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dms_call_count = 0
        self._stop_after = stop_after
        self.engine: GridEngine | None = None
        self.armed_at: datetime | None = None

    async def set_dead_mans_switch(self, timeout_seconds: int):  # type: ignore[no-untyped-def]
        self.dms_call_count += 1
        if self.dms_call_count == 1:
            self.armed_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
            return self.armed_at
        if self.dms_call_count >= self._stop_after and self.engine is not None:
            self.engine.request_stop()
        raise ExchangeError("simulated partial outage", codes=["EService:Unavailable"])


async def test_streak_start_logs_the_last_confirmed_deadline(
    storage: SQLiteStorageAdapter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2026-09-03 review, findings 5 + 7. Deleting the streak-start WARNING
    used to leave the whole suite green, and the alert's new
    ``last_confirmed_trigger_at`` was only ever asserted as None."""
    exch = _DmsArmsOnceThenFailsExchange(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
        stop_after=6,
    )
    engine = GridEngine(exch, storage, grid_config(), safety_config())
    exch.engine = engine
    notifier = SqliteNotifierAdapter(storage)

    with caplog.at_level(logging.WARNING, logger="wobblebot.cli.live"):
        await _run_loop(exch, engine, _live(), storage, asyncio.Event(), notifier=notifier)

    assert exch.armed_at is not None, "the fixture must confirm one arm"

    # The WARNING fires, exactly once per episode, and names a REAL deadline.
    streak_lines = [
        r.getMessage() for r in caplog.records if "failure streak started" in r.getMessage()
    ]
    assert len(streak_lines) == 1, streak_lines
    assert "last confirmed auto-cancel deadline" in streak_lines[0]
    assert exch.armed_at.strftime("%H:%M:%SZ") in streak_lines[0]

    # ...and the 3-strike page carries that same deadline, not None.
    rows = await storage.get_notifications()
    alerts = [r for r in rows if r.notification.title == "Dead-man's-switch resets failing"]
    assert len(alerts) == 1
    stamped = alerts[0].notification.context["last_confirmed_trigger_at"]
    assert isinstance(stamped, str) and stamped.startswith(exch.armed_at.strftime("%Y-%m-%d"))
    assert datetime.fromisoformat(stamped).tzinfo is not None, "must be tz-aware"


class _DmsFailsThenVanishesExchange(MockExchangeAdapter):
    """DMS reset fails from the first call; after ``vanish_after`` pings the
    book is cancelled on the EXCHANGE ONLY, so the next tick sees it gone.
    Drives the real ``_run_loop`` wiring rather than hand-setting escalation
    fields — the 2026-09-03 review's lesson was that a test pinning the
    message while stubbing its input does not pin the input."""

    def __init__(self, *args: object, vanish_after: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dms_call_count = 0
        self._vanish_after = vanish_after
        self.engine: GridEngine | None = None
        self.storage: SQLiteStorageAdapter | None = None
        self._vanished = False

    async def set_dead_mans_switch(self, timeout_seconds: int):  # type: ignore[no-untyped-def]
        self.dms_call_count += 1
        if self.dms_call_count >= self._vanish_after and not self._vanished:
            self._vanished = True
            assert self.storage is not None
            for order in await self.storage.get_open_orders(symbol=BTC_USD):
                self.inject_partial_cancel(order, filled_amount=Decimal("0"))
        elif self._vanished and self.engine is not None:
            self.engine.request_stop()
        raise ExchangeError("simulated stall", codes=["EService:Unavailable"])


async def test_the_vanish_page_carries_a_real_computed_window_fraction(
    storage: SQLiteStorageAdapter,
) -> None:
    """The elapsed-window fraction must be COMPUTED by the loop, not merely
    rendered. Hardcoding the per-tick snapshot to 0.0 has to fail here."""
    exch = _DmsFailsThenVanishesExchange(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
        vanish_after=3,
    )
    engine = GridEngine(exch, storage, grid_config(), safety_config())
    exch.engine = engine
    exch.storage = storage
    notifier = SqliteNotifierAdapter(storage)

    # The config floor is a 10s window, so slow the tick down until a handful
    # of them is a measurable fraction of it (~1.5s of 10s here). Worth the
    # couple of seconds: this is the only test that exercises the computation.
    await _run_loop(
        exch,
        engine,
        _live(tick_seconds=0.5, dead_mans_switch_seconds=10),
        storage,
        asyncio.Event(),
        notifier=notifier,
    )

    rows = await storage.get_notifications()
    vanish = [r for r in rows if r.notification.title.startswith("Book vanished")]
    assert vanish, "the fixture must actually vanish the book"
    frac = vanish[0].notification.context["dms_degraded_fraction"]
    assert frac > 0, "the loop must compute the fraction, not leave it at zero"

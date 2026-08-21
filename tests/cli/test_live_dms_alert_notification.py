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
from collections.abc import AsyncIterator
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

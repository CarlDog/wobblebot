"""v1.1 test-hardening (test-honesty audit 2026-06-02, P3 "Loss-cap
consequence E2E").

The per-tick loss-cap CHECK (``_run_one_tick`` comparing
``session_pnl < -max_session_loss_usd``) already had focused unit
coverage in ``test_shutdown_persistence.py``. What was missing: driving
the FULL ``_run_loop`` through a real trip and asserting the
CONSEQUENCE, not just the detection --

- ``exit_code == 1`` (not silently swallowed to 0, which a watchdog
  would read as "clean exit, safe to auto-restart into the same
  losing market").
- Every resting order actually gets canceled on the trip path (the
  ``finally`` block's ``_cancel_all_open`` call).
- The boundary is strictly ``<``, not ``<=`` -- a session_pnl of
  exactly ``-max_session_loss_usd`` must NOT trip.

A regression that skipped cancellation specifically on the cap-trip
branch, or that flipped the boundary to ``<=``, would ship green
against the pre-existing unit tests alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import live as live_module
from wobblebot.cli.live import _run_loop
from wobblebot.config.cli import LiveConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")

_STARTED_VALUE = Decimal("1000")
_CAP = Decimal("5")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _live_cfg() -> LiveConfig:
    return LiveConfig(
        symbols=[BTC_USD],
        db=":memory:",
        tick_seconds=5.0,
        max_runtime_minutes=None,
        max_session_loss_usd=_CAP,
        dead_mans_switch_seconds=None,
    )


async def _laid_out_engine(storage: SQLiteStorageAdapter) -> tuple[MockExchangeAdapter, GridEngine]:
    """A real engine with a real resting layout -- orders that must
    actually disappear on a clean cap-trip cancel, not just a
    count-based assertion."""
    exch = MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exch, storage, _grid_config(), _safety_config())
    await engine.step(BTC_USD)  # initializes + places the layout
    return exch, engine


class TestLossCapTripConsequence:
    async def test_trip_exits_1_and_cancels_every_resting_order(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exch, engine = await _laid_out_engine(storage)
        opens_before = await exch.get_open_orders()
        assert opens_before, "the layout should have placed resting orders"

        calls = {"n": 0}

        async def portfolio_value(_adapter: object, _symbols: object, _tickers: object = None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _STARTED_VALUE  # session-start value
            # Tripped: strictly worse than -cap (994 < 1000-5=995).
            return _STARTED_VALUE - _CAP - Decimal("1")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", portfolio_value)

        exit_code = await _run_loop(exch, engine, _live_cfg(), storage, asyncio.Event())

        assert exit_code == 1
        remaining = await exch.get_open_orders()
        assert remaining == [], "every resting order must be canceled on a cap trip"


class TestLossCapBoundary:
    """The check is session_pnl < -cap (strict). Exactly at -cap is a
    real, if extreme, drawdown the operator's cap explicitly allows --
    only breaching it should stop the session."""

    async def test_exactly_at_cap_does_not_trip(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exch, engine = await _laid_out_engine(storage)
        stop_event = asyncio.Event()
        calls = {"n": 0}

        async def portfolio_value(_adapter: object, _symbols: object, _tickers: object = None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _STARTED_VALUE
            # Exactly -cap: session_pnl == -5, NOT < -5. Also stop the
            # loop right after this tick so the test doesn't spin
            # forever on a real 5s tick_seconds sleep.
            stop_event.set()
            return _STARTED_VALUE - _CAP

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", portfolio_value)

        exit_code = await _run_loop(exch, engine, _live_cfg(), storage, stop_event)

        assert exit_code == 0
        # session-start + the one tick + the finally block's session-end
        # accounting call -- three calls total, no trip among them.
        assert calls["n"] == 3

    async def test_one_cent_past_cap_trips(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exch, engine = await _laid_out_engine(storage)
        calls = {"n": 0}

        async def portfolio_value(_adapter: object, _symbols: object, _tickers: object = None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _STARTED_VALUE
            # One cent past the boundary: session_pnl == -5.01.
            return _STARTED_VALUE - _CAP - Decimal("0.01")

        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", portfolio_value)

        exit_code = await _run_loop(exch, engine, _live_cfg(), storage, asyncio.Event())

        assert exit_code == 1

"""Tests for cli/observe._top_up_bars (P2 slice 3 follow-up).

The steady-state freshness half of the TA feature: once per hour the
poll loop resumes each symbol's 60m ohlc_bars from its interval-scoped
cursor. Pins:

- seed path: a pair with no bars fetches one TA-window's worth
- resume path: since = the latest stored 60m opened_at
- current path: newest completed bar stored -> NO Kraken call
- completed-bars-only: the in-progress hour bar is never persisted
  (INSERT OR IGNORE would freeze its partial values forever)
- fail-soft: adapter errors absorb per symbol, poll loop unaffected
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import StubOHLCAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.observe import _BAR_TOPUP_SEED_BARS, _top_up_bars
from wobblebot.config.cli import ObserveConfig
from wobblebot.domain.value_objects import OHLCBar, Symbol

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
# Mid-hour "now" so the completed/in-progress boundary is unambiguous.
_NOW = datetime(2026, 8, 8, 12, 30, 0, tzinfo=UTC)
_HOUR = timedelta(hours=1)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_bar(opened_at: datetime, symbol: Symbol = _BTC) -> OHLCBar:
    return OHLCBar(
        symbol=symbol,
        interval_minutes=60,
        opened_at=opened_at,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        vwap=Decimal("0"),
        volume=Decimal("1"),
        count=1,
    )


class TestTopUpBars:
    async def test_seed_path_fetches_ta_window(self, storage: SQLiteStorageAdapter) -> None:
        adapter = StubOHLCAdapter()
        await _top_up_bars(adapter, storage, [_BTC], now=_NOW)  # type: ignore[arg-type]
        assert len(adapter.calls) == 1
        _, interval, since = adapter.calls[0]
        assert interval == 60
        assert since == _NOW - _HOUR * _BAR_TOPUP_SEED_BARS

    async def test_resume_path_uses_cursor(self, storage: SQLiteStorageAdapter) -> None:
        cursor = _NOW.replace(minute=0) - 5 * _HOUR
        await storage.save_ohlc_bars([_make_bar(cursor)])
        adapter = StubOHLCAdapter()
        await _top_up_bars(adapter, storage, [_BTC], now=_NOW)  # type: ignore[arg-type]
        assert adapter.calls[0][2] == cursor

    async def test_current_cursor_skips_kraken_call(self, storage: SQLiteStorageAdapter) -> None:
        """Newest completed bar (11:00 for a 12:30 now) already stored:
        nothing to fetch, zero API burn."""
        await storage.save_ohlc_bars([_make_bar(_NOW.replace(minute=0) - _HOUR)])
        adapter = StubOHLCAdapter()
        await _top_up_bars(adapter, storage, [_BTC], now=_NOW)  # type: ignore[arg-type]
        assert adapter.calls == []

    async def test_in_progress_bar_not_persisted(self, storage: SQLiteStorageAdapter) -> None:
        """Kraken returns the completed 11:00 bar AND the in-progress
        12:00 bar; only the completed one may persist — the idempotent
        write path would freeze the partial values forever."""
        completed = _make_bar(_NOW.replace(minute=0) - _HOUR)
        in_progress = _make_bar(_NOW.replace(minute=0))
        adapter = StubOHLCAdapter([completed, in_progress])
        await _top_up_bars(adapter, storage, [_BTC], now=_NOW)  # type: ignore[arg-type]
        stored = await storage.get_ohlc_bars(_BTC, 60)
        assert [b.opened_at for b in stored] == [completed.opened_at]

    async def test_adapter_error_absorbed_per_symbol(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = StubOHLCAdapter(raise_error=True)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe"):
            await _top_up_bars(adapter, storage, [_BTC, _ETH], now=_NOW)  # type: ignore[arg-type]
        # Both symbols attempted despite the first failing.
        assert len(adapter.calls) == 2
        assert sum("bar top-up failed" in r.getMessage() for r in caplog.records) == 2


class TestConfigFlag:
    async def test_default_enabled(self) -> None:
        config = ObserveConfig(symbols=["BTC/USD"])
        assert config.bar_topup_enabled is True

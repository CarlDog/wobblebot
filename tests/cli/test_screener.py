"""Tests for cli/screener (P2 slice 5).

Wiring-level pins over real in-memory storage: discovery ("rank what
we have bars for"), the self-correlation exclusion, the skipped-thin
report, and the empty-DB exit path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.screener import _run, _screen
from wobblebot.config.cli import ScreenerConfig
from wobblebot.domain.value_objects import OHLCBar, Symbol

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_SOL = Symbol(base="SOL", quote="USD")
_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    closes: list[float],
) -> None:
    bars = []
    prev = closes[0]
    start = _NOW - timedelta(hours=len(closes))
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.5
        low = min(prev, close) - 0.5
        bars.append(
            OHLCBar(
                symbol=symbol,
                interval_minutes=60,
                opened_at=start + timedelta(hours=i),
                open=Decimal(str(prev)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
        )
        prev = close
    await storage.save_ohlc_bars(bars)


def _wavy(n: int, base: float = 100.0) -> list[float]:
    return [base + ((i * 3) % 7) for i in range(n)]


class TestScreen:
    async def test_ranks_and_annotates(self, storage: SQLiteStorageAdapter) -> None:
        await _seed(storage, _BTC, _wavy(120))
        await _seed(storage, _ETH, _wavy(120, base=200.0))
        await _seed(storage, _SOL, _wavy(120, base=50.0))
        config = ScreenerConfig(lookback_days=30)
        rankings, skipped = await _screen(
            storage, config, symbols=[_BTC, _ETH, _SOL], held=[_BTC], now=_NOW
        )
        assert len(rankings) == 3
        assert skipped == []
        # ETH and SOL share BTC's wave shape -> strong correlation vs held BTC.
        by_symbol = {r.metrics.symbol: r for r in rankings}
        assert by_symbol[_ETH].max_correlation == pytest.approx(1.0, abs=1e-3)
        assert by_symbol[_ETH].correlated_with == _BTC

    async def test_candidate_never_correlates_with_itself(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """BTC is both a candidate and the only held symbol: its
        annotation must be n/a, not a meaningless +1.00 vs itself."""
        await _seed(storage, _BTC, _wavy(120))
        config = ScreenerConfig(lookback_days=30)
        rankings, _ = await _screen(storage, config, symbols=[_BTC], held=[_BTC], now=_NOW)
        assert rankings[0].max_correlation is None
        assert rankings[0].correlated_with is None

    async def test_thin_symbol_reported_skipped(self, storage: SQLiteStorageAdapter) -> None:
        await _seed(storage, _BTC, _wavy(120))
        await _seed(storage, _ETH, _wavy(5))  # under MIN_BARS
        config = ScreenerConfig(lookback_days=30)
        rankings, skipped = await _screen(storage, config, symbols=[_BTC, _ETH], held=[], now=_NOW)
        assert [r.metrics.symbol for r in rankings] == [_BTC]
        assert skipped == [_ETH]

    async def test_lookback_window_limits_bars(self, storage: SQLiteStorageAdapter) -> None:
        """Bars older than the lookback don't count toward MIN_BARS."""
        old_start = _NOW - timedelta(days=90)
        old_bars = []
        prev = 100.0
        for i in range(120):
            close = 100.0 + (i % 5)
            old_bars.append(
                OHLCBar(
                    symbol=_BTC,
                    interval_minutes=60,
                    opened_at=old_start + timedelta(hours=i),
                    open=Decimal(str(prev)),
                    high=Decimal(str(max(prev, close) + 0.5)),
                    low=Decimal(str(min(prev, close) - 0.5)),
                    close=Decimal(str(close)),
                    vwap=Decimal("0"),
                    volume=Decimal("1"),
                    count=1,
                )
            )
            prev = close
        await storage.save_ohlc_bars(old_bars)
        config = ScreenerConfig(lookback_days=30)
        rankings, skipped = await _screen(storage, config, symbols=[_BTC], held=[], now=_NOW)
        assert rankings == []
        assert skipped == [_BTC]


class TestRun:
    async def test_empty_db_exits_1(self, storage: SQLiteStorageAdapter) -> None:
        config = ScreenerConfig(db=":memory:")
        assert await _run(config, symbols_override=None, held=[]) == 1

    async def test_discovery_ranks_stored_symbols(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        db_path = str(tmp_path / "screener-test.db")
        seed_storage = SQLiteStorageAdapter(db_path)
        await seed_storage.connect()
        await _seed(seed_storage, _BTC, _wavy(120))
        await seed_storage.close()
        config = ScreenerConfig(db=db_path)
        assert await _run(config, symbols_override=None, held=[]) == 0

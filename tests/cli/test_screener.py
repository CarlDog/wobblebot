"""Tests for cli/screener (P2 slice 5).

Wiring-level pins over real in-memory storage: discovery ("rank what
we have bars for"), the self-correlation exclusion, the skipped-thin
report, and the empty-DB exit path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from tests.fixtures import bars_from_closes
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.screener import _run, _screen, main
from wobblebot.config.cli import ScreenerConfig
from wobblebot.domain.value_objects import Symbol

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
    *,
    base: datetime = _NOW,
) -> None:
    """Seed hourly bars ending at ``base``.

    The fixed ``_NOW`` default is CORRECT for the ``TestScreen`` cases —
    they pass ``now=_NOW`` into ``_screen``, so fixture and window share
    one clock and never drift. A test that exercises the real-clock path
    (``_run`` has no ``now=`` parameter) must seed ``base=datetime.now``
    instead: the 2026-08-16 time-bomb audit found the discovery test
    seeding at fixed dates against a real-clock 30d lookback, which
    would have started failing ~2026-09-07 on nothing but the calendar.
    """
    start = base - timedelta(hours=len(closes))
    await storage.save_ohlc_bars(bars_from_closes(closes, symbol=symbol, start=start, spread=0.5))


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
        old_closes = [100.0 + (i % 5) for i in range(120)]
        await storage.save_ohlc_bars(
            bars_from_closes(old_closes, symbol=_BTC, start=old_start, spread=0.5)
        )
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
        # _run uses the REAL clock (no now= parameter), so the bars must
        # be seeded relative to it — see _seed's docstring.
        await _seed(seed_storage, _BTC, _wavy(120), base=datetime.now(UTC))
        await seed_storage.close()
        config = ScreenerConfig(db=db_path)
        assert await _run(config, symbols_override=None, held=[]) == 0


class TestMainDeprivedEnv:
    """The fleet contract: a missing per-CLI section exits 2 with a
    LOGGED error, not a raw stderr write. screener was the one CLI of
    sixteen that diverged (it read log_format from the section it was
    about to find missing, so it wrote to stderr pre-logging instead —
    aligned 2026-08-15 by defaulting the format first)."""

    async def test_missing_section_exits_2_via_logger(
        self,
        tmp_path,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = tmp_path / "settings.yml"
        cfg.write_text(
            "grid:\n"
            "  default:\n"
            "    spacing_percentage: 1.0\n"
            "    levels_above: 3\n"
            "    levels_below: 3\n"
            "    order_size_usd: 10\n"
            "safety:\n"
            "  max_total_exposure_usd: 60\n"
            "  max_per_coin_exposure_usd: 60\n"
            "  max_daily_spend_usd: 60\n"
            "  max_orders_per_coin: 6\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("sys.argv", ["screener", "--config", str(cfg)])

        assert main() == 2

        # The message must arrive through the configured LOGGER (note the
        # [ERROR] level tag from the formatter), not a raw stderr write —
        # so JSON log mode and the rotating-file handler both see it.
        err = capsys.readouterr().err
        assert "missing the `screener:` section" in err
        assert "[ERROR]" in err

"""cli/live's sweep ordering — the wiring, not the scoring.

``services/symbol_priority`` is pure and pinned in its own module. What
these cover is everything between that function and a real tick: does
``_run_one_tick`` actually STEP in the order it was handed, does the
refresh cache instead of re-ranking every 5 seconds, and does every
failure path degrade to "trade in config order" rather than to "stop
trading"?

That last property is the whole reason for the paranoia. Ordering is a
preference; a bug in a preference must never be able to take down a
real-money loop, and — because the loop would simply keep sweeping in
config order — such a bug is silent by construction.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from tests.fixtures import bars_from_closes
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import live as live_module
from wobblebot.cli.live import (
    _SWEEP_REFRESH_SECONDS,
    _build_screener_inputs,
    _open_observe_storage,
    _refresh_sweep_order,
    _run_one_tick,
    format_sweep,
)
from wobblebot.config.cli import LiveConfig, ScreenerConfig
from wobblebot.domain.grid import GridState
from wobblebot.domain.value_objects import Symbol, Ticker, Timestamp
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.services.screener import ScreenerRanking, SymbolMetrics

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")
_SOL = Symbol(base="SOL", quote="USD")

_SCREENER = ScreenerConfig()


def _ticker(symbol: Symbol, last: str = "50000") -> Ticker:
    return Ticker(symbol=symbol, last=Decimal(last), bid=Decimal(last) - 1, ask=Decimal(last) + 1)


def _live_cfg(symbols: list[Symbol], **kwargs: Any) -> LiveConfig:
    return LiveConfig(
        symbols=symbols,
        db=":memory:",
        tick_seconds=5.0,
        max_runtime_minutes=None,
        max_session_loss_usd=Decimal("150"),
        **kwargs,
    )


def _tick_adapter() -> MagicMock:
    """An adapter that makes a tick succeed with nothing to do."""
    adapter = MagicMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.get_ticker = AsyncMock(side_effect=_ticker)
    return adapter


def _idle_engine() -> MagicMock:
    engine = MagicMock()
    engine.step = AsyncMock(return_value=MagicMock(action="stepped", fills=0))
    engine.has_pending_fill_candidates = AsyncMock(return_value=False)
    return engine


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def observe() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed_bars(observe_storage: SQLiteStorageAdapter, symbol: Symbol, count: int) -> None:
    """``count`` hourly bars ending an hour ago, so they land inside any
    lookback window regardless of when the suite runs."""
    closes = [100.0 + (i % 7) * 0.5 for i in range(count)]
    await observe_storage.save_ohlc_bars(
        bars_from_closes(
            closes,
            symbol=symbol,
            start=datetime.now(UTC) - timedelta(hours=count),
            interval_minutes=60,
            spread=0.25,
        )
    )


async def _anchor(
    live_storage: SQLiteStorageAdapter, symbol: Symbol, reference: str = "100"
) -> None:
    await live_storage.save_grid_state(
        GridState(
            symbol=symbol,
            reference_price=Decimal(reference),
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )


# --------------------------------------------------------------------------- #
# _run_one_tick: the sweep is the STEP order                                   #
# --------------------------------------------------------------------------- #


class TestTickHonorsSweep:
    """The measured bug: config order gave the first-listed symbol first
    claim on the caps every tick (ETH 5/6 -> SOL 2/6 -> ADA 0/6, straight
    down the list). Ordering only matters if the step loop actually uses
    it, so that is pinned here rather than inferred from the scorer."""

    async def test_steps_in_the_supplied_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", _no_trip)
        engine = _idle_engine()

        await _run_one_tick(
            adapter=_tick_adapter(),
            engine=engine,
            live=_live_cfg([_BTC, _ETH, _SOL]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
            sweep=[_SOL, _ETH, _BTC],
        )

        assert [call.args[0] for call in engine.step.await_args_list] == [_SOL, _ETH, _BTC]

    async def test_none_falls_back_to_config_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The DEFAULT strategy's path. An upgrade must change nothing
        for an operator who never opts in."""
        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", _no_trip)
        engine = _idle_engine()

        await _run_one_tick(
            adapter=_tick_adapter(),
            engine=engine,
            live=_live_cfg([_BTC, _ETH, _SOL]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
            sweep=None,
        )

        assert [call.args[0] for call in engine.step.await_args_list] == [_BTC, _ETH, _SOL]

    async def test_sweep_reorders_but_never_drops_a_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sweep that lost a symbol would silently stop trading it —
        the same class of quiet failure this whole feature exists to fix."""
        monkeypatch.setattr(live_module, "_session_portfolio_value_usd", _no_trip)
        engine = _idle_engine()

        await _run_one_tick(
            adapter=_tick_adapter(),
            engine=engine,
            live=_live_cfg([_BTC, _ETH, _SOL]),
            tick=1,
            started_value_usd=Decimal("100"),
            notifier=None,
            sweep=[_ETH, _SOL, _BTC],
        )

        assert {call.args[0] for call in engine.step.await_args_list} == {_BTC, _ETH, _SOL}


async def _no_trip(_adapter: Any, _symbols: Any, _tickers: Any = None) -> Decimal:
    return Decimal("100")


# --------------------------------------------------------------------------- #
# _refresh_sweep_order: strategy dispatch, caching, and the degrade paths      #
# --------------------------------------------------------------------------- #


class TestRefreshSweepOrder:
    async def test_config_order_reads_nothing(self, storage: SQLiteStorageAdapter) -> None:
        """The default must stay free: no ranking, no storage reads, and
        a None order so the tick loop uses live.symbols directly."""
        adapter = MagicMock()
        order, computed_at = await _refresh_sweep_order(
            live=_live_cfg([_BTC, _ETH]),
            screener=_SCREENER,
            storage=storage,
            observe_storage=None,
            adapter=adapter,
            tick=7,
            current=None,
            computed_at=None,
        )
        assert order is None
        assert computed_at is None
        adapter.get_ticker.assert_not_called()

    async def test_round_robin_rotates_per_tick(self, storage: SQLiteStorageAdapter) -> None:
        live = _live_cfg([_BTC, _ETH, _SOL], symbol_priority="round_robin")
        orders = []
        for tick in range(3):
            order, _ = await _refresh_sweep_order(
                live=live,
                screener=_SCREENER,
                storage=storage,
                observe_storage=None,
                adapter=MagicMock(),
                tick=tick,
                current=None,
                computed_at=None,
            )
            orders.append(order)
        assert orders == [[_BTC, _ETH, _SOL], [_ETH, _SOL, _BTC], [_SOL, _BTC, _ETH]]

    async def test_screener_ranks_the_cohort(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        await _seed_bars(observe, _BTC, 40)
        await _seed_bars(observe, _ETH, 40)
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

        order, computed_at = await _refresh_sweep_order(
            live=_live_cfg([_BTC, _ETH], symbol_priority="screener", observe_db=":memory:"),
            screener=_SCREENER,
            storage=storage,
            observe_storage=observe,
            adapter=adapter,
            tick=1,
            current=None,
            computed_at=None,
        )

        assert order is not None
        assert sorted(str(s) for s in order) == ["BTC/USD", "ETH/USD"]
        assert computed_at is not None

    async def test_screener_caches_within_the_refresh_window(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        """The inputs are 60m bars. Re-ranking every 5s would spend reads
        for an identical answer AND risk an order that wobbles tick to
        tick, which makes 'why did SOL get funded and ADA not'
        unanswerable after the fact."""
        await _seed_bars(observe, _BTC, 40)
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))
        live = _live_cfg([_BTC], symbol_priority="screener", observe_db=":memory:")

        first, at = await _refresh_sweep_order(
            live=live,
            screener=_SCREENER,
            storage=storage,
            observe_storage=observe,
            adapter=adapter,
            tick=1,
            current=None,
            computed_at=None,
        )
        calls_after_first = adapter.get_ticker.await_count

        again, at_again = await _refresh_sweep_order(
            live=live,
            screener=_SCREENER,
            storage=storage,
            observe_storage=observe,
            adapter=adapter,
            tick=2,
            current=first,
            computed_at=at,
        )

        assert again == first
        assert at_again == at
        assert adapter.get_ticker.await_count == calls_after_first, "cached tick re-ranked"

    async def test_screener_recomputes_once_the_window_lapses(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        await _seed_bars(observe, _BTC, 40)
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))
        live = _live_cfg([_BTC], symbol_priority="screener", observe_db=":memory:")

        stale = -_SWEEP_REFRESH_SECONDS  # monotonic() - stale > the window
        order, _ = await _refresh_sweep_order(
            live=live,
            screener=_SCREENER,
            storage=storage,
            observe_storage=observe,
            adapter=adapter,
            tick=2,
            current=[_BTC],
            computed_at=stale,
        )

        assert order == [_BTC]
        assert adapter.get_ticker.await_count == 1

    async def test_missing_observe_db_degrades_to_config_order(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Config validation pairs screener with observe_db, so reaching
        this means the DB failed to OPEN. Keep trading."""
        order, computed_at = await _refresh_sweep_order(
            live=_live_cfg([_BTC, _ETH], symbol_priority="screener", observe_db=":memory:"),
            screener=_SCREENER,
            storage=storage,
            observe_storage=None,
            adapter=MagicMock(),
            tick=1,
            current=None,
            computed_at=None,
        )
        assert order is None
        assert computed_at is not None, "the failure is still rate-limited to hourly"

    async def test_a_scoring_failure_keeps_the_previous_order(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter, monkeypatch: Any
    ) -> None:
        """A bug in a PREFERENCE must not stop a real-money loop."""

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise ZeroDivisionError("scoring bug")

        monkeypatch.setattr(live_module, "_build_screener_inputs", _boom)
        previous = [_ETH, _BTC]

        order, computed_at = await _refresh_sweep_order(
            live=_live_cfg([_BTC, _ETH], symbol_priority="screener", observe_db=":memory:"),
            screener=_SCREENER,
            storage=storage,
            observe_storage=observe,
            adapter=MagicMock(),
            tick=1,
            current=previous,
            computed_at=None,
        )

        assert order == previous
        assert computed_at is not None, "a failing refresh must not retry every tick"


# --------------------------------------------------------------------------- #
# _build_screener_inputs: every read degrades to "no opinion"                  #
# --------------------------------------------------------------------------- #


class TestBuildScreenerInputs:
    async def test_ranks_symbols_with_enough_bars(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        await _seed_bars(observe, _BTC, 40)
        await _seed_bars(observe, _ETH, 40)
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

        rankings, proximity = await _build_screener_inputs(
            [_BTC, _ETH], storage, observe, adapter, _SCREENER
        )

        assert {r.metrics.symbol for r in rankings} == {_BTC, _ETH}
        assert set(proximity) == {_BTC, _ETH}

    async def test_thin_bars_go_unranked_but_stay_in_the_cohort(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        """Unranked means unknown, and unknown sorts last — but the
        symbol must still appear, or ordering would silently drop it."""
        await _seed_bars(observe, _BTC, 40)
        await _seed_bars(observe, _ETH, 5)  # below MIN_BARS
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

        rankings, proximity = await _build_screener_inputs(
            [_BTC, _ETH], storage, observe, adapter, _SCREENER
        )

        assert {r.metrics.symbol for r in rankings} == {_BTC}
        assert proximity[_ETH] == math.inf
        assert set(proximity) == {_BTC, _ETH}

    async def test_anchored_symbol_gets_a_finite_proximity(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        await _seed_bars(observe, _BTC, 40)
        await _anchor(storage, _BTC, "100")  # levels every 1% -> 99, 101, ...
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100.9"))

        _, proximity = await _build_screener_inputs([_BTC], storage, observe, adapter, _SCREENER)

        assert math.isfinite(proximity[_BTC])

    async def test_closer_to_a_level_scores_lower(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        """Lower = nearer a fill. The direction is the contract; the
        absolute magnitude is ATR-scaled and not worth pinning."""
        await _seed_bars(observe, _BTC, 40)
        await _anchor(storage, _BTC, "100")
        near = MagicMock()
        near.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100.99"))
        far = MagicMock()
        far.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100.00"))

        _, near_prox = await _build_screener_inputs([_BTC], storage, observe, near, _SCREENER)
        _, far_prox = await _build_screener_inputs([_BTC], storage, observe, far, _SCREENER)

        assert near_prox[_BTC] < far_prox[_BTC]

    async def test_no_grid_anchor_is_unknowable_not_imminent(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        """A symbol the engine has never stepped has no ladder. inf, not
        0 — missing data must never be mistaken for 'about to fill'."""
        await _seed_bars(observe, _BTC, 40)
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

        _, proximity = await _build_screener_inputs([_BTC], storage, observe, adapter, _SCREENER)

        assert proximity[_BTC] == math.inf

    async def test_ticker_failure_costs_the_tiebreak_not_the_rank(
        self, storage: SQLiteStorageAdapter, observe: SQLiteStorageAdapter
    ) -> None:
        await _seed_bars(observe, _BTC, 40)
        await _anchor(storage, _BTC, "100")
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=ExchangeError("EAPI:Rate limit exceeded"))

        rankings, proximity = await _build_screener_inputs(
            [_BTC], storage, observe, adapter, _SCREENER
        )

        assert {r.metrics.symbol for r in rankings} == {_BTC}
        assert proximity[_BTC] == math.inf

    async def test_a_bar_read_failure_skips_only_that_symbol(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        class _BrokenBars(SQLiteStorageAdapter):
            async def get_ohlc_bars(self, *args: Any, **kwargs: Any) -> Any:
                raise StorageError("observe.db unreadable")

        broken = _BrokenBars(":memory:")
        await broken.connect()
        try:
            adapter = MagicMock()
            adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

            rankings, proximity = await _build_screener_inputs(
                [_BTC, _ETH], storage, broken, adapter, _SCREENER
            )

            assert rankings == []
            assert set(proximity) == {_BTC, _ETH}
        finally:
            await broken.close()

    async def test_a_grid_state_failure_does_not_raise(self, observe: SQLiteStorageAdapter) -> None:
        class _BrokenGridState(SQLiteStorageAdapter):
            async def get_grid_state(self, symbol: Symbol) -> Any:
                raise StorageError("live.db unreadable")

        broken = _BrokenGridState(":memory:")
        await broken.connect()
        try:
            await _seed_bars(observe, _BTC, 40)
            adapter = MagicMock()
            adapter.get_ticker = AsyncMock(side_effect=lambda s: _ticker(s, "100"))

            rankings, proximity = await _build_screener_inputs(
                [_BTC], broken, observe, adapter, _SCREENER
            )

            assert {r.metrics.symbol for r in rankings} == {_BTC}
            assert proximity[_BTC] == math.inf
        finally:
            await broken.close()


# --------------------------------------------------------------------------- #
# format_sweep: a reorder must be explainable from the log alone               #
# --------------------------------------------------------------------------- #


def _rank(symbol: Symbol, composite: float) -> ScreenerRanking:
    return ScreenerRanking(
        metrics=SymbolMetrics(
            symbol=symbol, volatility=0.004, flatness=0.9, atr_pct=0.5, bar_count=400
        ),
        vol_rank=1,
        flatness_rank=1,
        atr_rank=1,
        composite=composite,
    )


class TestFormatSweep:
    """Logging the order alone made every reorder unexplainable — working
    out why one 2026-08-15 change happened took copies of two production
    DBs and a replay script. Two consecutive log lines should have shown
    it, so the scores go in the message."""

    async def test_renders_composite_and_proximity_in_order(self) -> None:
        line = format_sweep(
            [_SOL, _ETH, _BTC],
            [_rank(_SOL, 2.6667), _rank(_ETH, 3.3333), _rank(_BTC, 4.0)],
            {_SOL: 8.315, _ETH: 12.871, _BTC: 2.385},
        )
        assert line == "SOL/USD(2.67|8.3) > ETH/USD(3.33|12.9) > BTC/USD(4.00|2.4)"

    async def test_diffing_two_lines_names_the_symbol_that_moved(self) -> None:
        """The real 2026-08-15 reorder. SOL's composite went 2.67 -> 3.00
        on one new hourly bar; nothing else changed. That has to be
        readable by eye from consecutive lines, or the log is decoration."""
        before = format_sweep(
            [_SOL, _ETH, _BTC],
            [_rank(_SOL, 2.6667), _rank(_ETH, 3.3333), _rank(_BTC, 4.0)],
            {_SOL: 8.3, _ETH: 12.9, _BTC: 2.4},
        )
        after = format_sweep(
            [_ETH, _SOL, _BTC],
            [_rank(_SOL, 3.0), _rank(_ETH, 3.3333), _rank(_BTC, 4.0)],
            {_SOL: 8.3, _ETH: 12.9, _BTC: 2.4},
        )
        assert "SOL/USD(2.67|" in before
        assert "SOL/USD(3.00|" in after
        assert "ETH/USD(3.33|" in before and "ETH/USD(3.33|" in after, "ETH must look unchanged"

    async def test_exact_ties_are_visible_so_the_tiebreak_reads_as_deliberate(self) -> None:
        """Composite is a mean of three integer ranks, so exact ties are
        common. Without both numbers a tie-broken order looks like an
        arbitrary swap between equal scores."""
        line = format_sweep(
            [_SOL, _ETH],
            [_rank(_SOL, 2.6667), _rank(_ETH, 2.6667)],
            {_SOL: 8.3, _ETH: 87.1},
        )
        assert line == "SOL/USD(2.67|8.3) > ETH/USD(2.67|87.1)"

    async def test_unranked_shows_na_not_a_fabricated_score(self) -> None:
        """It sorted last because it is UNKNOWN. A number here would hide
        that — the same failure as proximity returning 0 instead of inf."""
        line = format_sweep([_SOL, _BTC], [_rank(_SOL, 2.0)], {_SOL: 1.0})
        assert line == "SOL/USD(2.00|1.0) > BTC/USD(n/a|inf)"

    async def test_empty_sweep_is_not_an_error(self) -> None:
        assert format_sweep([], [], {}) == ""


# --------------------------------------------------------------------------- #
# _open_observe_storage: a data-collection DB must not gate trading            #
# --------------------------------------------------------------------------- #


class TestOpenObserveStorage:
    async def test_unset_path_opens_nothing(self) -> None:
        assert await _open_observe_storage(None) is None

    async def test_unopenable_path_degrades_instead_of_raising(self, tmp_path: Any) -> None:
        """A directory is not a database. cli/live must still trade —
        the money path cannot depend on the observe DB being reachable."""
        assert await _open_observe_storage(str(tmp_path)) is None

    async def test_a_real_path_opens(self, tmp_path: Any) -> None:
        opened = await _open_observe_storage(str(tmp_path / "observe.db"))
        assert opened is not None
        await opened.close()

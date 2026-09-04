"""Tests for cli/live's engine_state publishing (ADR-030, P3 slice 3).

Pins the load-bearing choices: state comes from the ENGINE accessors
(a paused symbol reports paused even though its StepResult.offside is
False), a failed grid-state read degrades reference_price/anchored_at
to None without dropping the row, and an unwired operator_db is a
total no-op.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _emit_engine_states, _restore_engine_state
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000")},
        starting_prices={BTC_USD: Decimal("50000"), ETH_USD: Decimal("3000")},
    )


class TestEmitEngineStates:
    async def test_one_row_per_symbol_with_anchor_data(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # anchors at 50000

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.symbol == BTC_USD
        assert row.paused is False
        assert row.offside is False
        assert row.offside_ticks == 0
        assert row.reference_price == Decimal("50000")
        assert row.anchored_at is not None

    async def test_paused_symbol_reports_paused(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The trap this design avoids: a paused symbol's StepResult
        carries offside=False and no pause flag — state must come from
        the engine accessors."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        engine.pause_symbol(BTC_USD)
        await engine.step(BTC_USD)  # skipped_paused

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.paused is True

    async def test_offside_symbol_reports_ticks(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("48000"))  # below the band
        await engine.step(BTC_USD)

        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.offside is True
        assert row.offside_ticks >= 1

    async def test_pre_anchor_symbol_writes_nullable_row(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A symbol that never stepped still gets a row (paused=False,
        offside=False) with honest None anchor fields."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())

        await _emit_engine_states(engine, [ETH_USD], storage, operator_storage)

        [row] = await operator_storage.get_engine_states()
        assert row.symbol == ETH_USD
        assert row.reference_price is None
        assert row.anchored_at is None

    async def test_unwired_operator_db_is_noop(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        # Must not raise, must not read grid state.
        await _emit_engine_states(engine, [BTC_USD], storage, None)

    async def test_grid_state_read_failure_degrades_not_drops(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A broken live.db read must still publish paused/offside —
        the load-bearing visibility — with anchor fields None."""
        from wobblebot.ports.exceptions import StorageError

        class _BrokenGridStateStorage(SQLiteStorageAdapter):
            async def get_grid_state(self, symbol):  # type: ignore[no-untyped-def]
                raise StorageError("live.db unreadable")

        broken = _BrokenGridStateStorage(":memory:")
        await broken.connect()
        try:
            engine = GridEngine(_exchange(), broken, _grid_config(), _safety_config())
            engine.pause_symbol(BTC_USD)

            await _emit_engine_states(engine, [BTC_USD], broken, operator_storage)

            [row] = await operator_storage.get_engine_states()
            assert row.paused is True
            assert row.reference_price is None
        finally:
            await broken.close()


class TestRestorePausedSymbols:
    """Pause used to live only in process memory, so every restart of
    cli/live silently resumed trading on a symbol the operator had
    deliberately stopped. The state was already written to engine_state
    each tick for the dashboard (ADR-030) — nothing read it back.

    These pin the restore, and especially the two judgement calls in it:
    a STALE row must still restore (a pause is intent, not a cache), and
    an unconfigured symbol must not.
    """

    async def test_restores_a_paused_symbol(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD, ETH_USD], storage, operator_storage)

        # A fresh process: new engine, nothing paused in memory.
        restarted = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        assert restarted.is_paused(BTC_USD) is False
        await _restore_engine_state(restarted, [BTC_USD, ETH_USD], operator_storage)

        assert restarted.is_paused(BTC_USD) is True
        assert restarted.is_paused(ETH_USD) is False

    async def test_a_stale_pause_still_restores(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The load-bearing judgement call. get_engine_states leaves the
        freshness guard to consumers so the dashboard can't render a dead
        engine as live — but restore wants the opposite. Expiring a pause
        silently resumes trading, which is the bug, not the fix."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        # Backdate the row a week — far past any dashboard freshness window.
        conn = operator_storage._require_conn()  # pylint: disable=protected-access
        await conn.execute(
            "UPDATE engine_state SET updated_at = ? WHERE symbol_base = 'BTC'",
            ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
        )
        await conn.commit()

        restarted = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        assert restarted.is_paused(BTC_USD) is True, "a week-old pause is still a pause"

    async def test_ignores_a_symbol_no_longer_configured(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(ETH_USD)
        await _emit_engine_states(engine, [BTC_USD, ETH_USD], storage, operator_storage)

        restarted = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)  # ETH dropped
        assert restarted.is_paused(ETH_USD) is False

    async def test_unwired_operator_db_is_a_noop(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _restore_engine_state(engine, [BTC_USD], None)
        assert engine.is_paused(BTC_USD) is False

    async def test_an_active_symbol_is_not_paused_by_restore(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The inverse failure: restore must never INVENT a pause."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        restarted = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        assert restarted.is_paused(BTC_USD) is False


class TestOffsideSinceRoundTrip:
    """The honest-unknown rule, end to end through storage.

    ``offside_since`` exists so a duration is wall-clock truth rather than
    "since cli/live last started". That only holds if a restart re-seeds
    the running episode — otherwise the first tick reads as a fresh
    transition and stamps the boot time, turning a symbol parked for weeks
    into one parked for seconds on every deploy.
    """

    async def _park_btc(
        self, storage: SQLiteStorageAdapter
    ) -> tuple[GridEngine, MockExchangeAdapter]:
        """Anchor BTC, then move price far above the band so it goes offside.

        Returns the exchange too: a restart replaces the process, not
        Kraken, so a restarted engine must be built on the SAME exchange or
        fill detection cannot find the orders storage already knows about.
        """
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # anchors at 50000
        exchange.set_price(BTC_USD, Decimal("90000"))
        await engine.step(BTC_USD)  # offside, transition observed
        assert engine.offside_ticks(BTC_USD) >= 1
        return engine, exchange

    async def test_a_witnessed_transition_stamps_a_start(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine, exchange = await self._park_btc(storage)
        assert engine.offside_since(BTC_USD) is not None
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is True
        assert row.offside_since == engine.offside_since(BTC_USD)

    async def test_a_restart_keeps_the_original_start(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The whole feature. Without the restore seed the restarted engine
        sees tick 1 as a fresh transition and stamps NOW."""
        engine, exchange = await self._park_btc(storage)
        original = engine.offside_since(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        await restarted.step(BTC_USD)
        assert restarted.offside_since(BTC_USD) == original

    async def test_an_unobserved_start_stays_unknown_forever(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A row that predates the column carries offside=1 with no start.
        BTC and ETH are exactly this in production. The restarted engine
        must keep saying "unknown" rather than stamping its own boot time —
        `prev.since or now` instead of `prev.since if prev is not None`
        would silently claim the symbol had just gone offside.
        """
        engine, exchange = await self._park_btc(storage)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        conn = operator_storage._require_conn()  # pylint: disable=protected-access
        await conn.execute("UPDATE engine_state SET offside_since = NULL")
        await conn.commit()

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        for _ in range(3):
            await restarted.step(BTC_USD)
        assert restarted.offside_since(BTC_USD) is None
        await _emit_engine_states(restarted, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is True
        assert row.offside_since is None, "an unknown start must never acquire a value"

    async def test_coming_back_onside_clears_the_start(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        engine, exchange = await self._park_btc(storage)
        assert engine.offside_since(BTC_USD) is not None
        exchange.set_price(BTC_USD, Decimal("50000"))  # back inside the band
        await engine.step(BTC_USD)
        assert engine.offside_since(BTC_USD) is None
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is False
        assert row.offside_since is None

    async def test_a_new_episode_gets_a_new_start(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Coming back onside must not leave the previous episode's start
        behind for the next one to inherit."""
        engine, exchange = await self._park_btc(storage)
        first = engine.offside_since(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("50000"))
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("90000"))
        await engine.step(BTC_USD)
        second = engine.offside_since(BTC_USD)
        assert second is not None
        assert first is not None
        assert second > first

    async def test_restore_does_not_seed_an_onside_symbol(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The inverse failure, matching the pause tests: restore must never
        INVENT an offside episode."""
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        restarted = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        assert restarted.offside_ticks(BTC_USD) == 0
        assert restarted.offside_since(BTC_USD) is None

    async def test_a_reanchor_ends_the_episode(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """A re-anchor rebuilds the band around current price, so whatever
        episode was running is over — including its start."""
        engine, exchange = await self._park_btc(storage)
        assert engine.offside_since(BTC_USD) is not None
        ok, _ = await engine.request_reanchor(BTC_USD)
        assert ok is True
        assert engine.offside_since(BTC_USD) is None

    async def test_a_stale_restored_episode_is_corrected_by_the_first_tick(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Why restoring an unbounded-age row is safe here, unlike a pause.

        A pause is operator intent and must survive any gap. An offside
        episode is a fact about price, so the seed is falsifiable: the
        daemon can be down for days while price returns to the band, and
        the first tick recomputes ``is_offside`` and drops the episode.
        Without that, a stale row would keep a card marked OFFSIDE with a
        confident start time for a symbol that is trading normally.
        """
        engine, exchange = await self._park_btc(storage)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is True and row.offside_since is not None

        # Days later: price is back inside the band before the daemon restarts.
        exchange.set_price(BTC_USD, Decimal("50000"))
        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        assert restarted.offside_since(BTC_USD) is not None, "seeded from the stale row"

        await restarted.step(BTC_USD)
        assert restarted.offside_ticks(BTC_USD) == 0
        assert restarted.offside_since(BTC_USD) is None
        await _emit_engine_states(restarted, [BTC_USD], storage, operator_storage)
        [after] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert after.offside is False
        assert after.offside_since is None

    async def test_a_degraded_zero_tick_count_does_not_erase_a_paused_episode(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """offside=1 with offside_ticks=0 is reachable — get_engine_states
        degrades an unparseable count to 0 rather than dropping the row, so
        as not to lose a pause with it.

        The paused case is what makes the floor load-bearing. The emit
        derives the persisted flag as ``offside = ticks > 0``, and a paused
        symbol's tick returns at the pause gate without touching the
        counter. Restored verbatim at 0, the very next emit would write
        offside=False and erase a start that had been recorded for weeks —
        a corrupt integer silently deleting a different column's truth.
        """
        engine, exchange = await self._park_btc(storage)
        original = engine.offside_since(BTC_USD)
        engine.pause_symbol(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        conn = operator_storage._require_conn()  # pylint: disable=protected-access
        await conn.execute("UPDATE engine_state SET offside_ticks = 0")
        await conn.commit()

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage)
        assert restarted.is_paused(BTC_USD) is True
        await restarted.step(BTC_USD)  # returns at the pause gate
        await _emit_engine_states(restarted, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is True
        assert row.offside_since == original

    async def test_a_disabled_coin_is_not_seeded(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The seed is only safe because the next tick can falsify it — and
        a disabled coin's tick never runs.

        ``_step_unlocked`` returns ``skipped_disabled`` before the
        ``is_offside`` recompute, and ``enabled: false`` is the documented
        way to stop a coin while leaving it in ``live.symbols``. Seeding one
        would leave a permanent OFFSIDE badge over a "Parked since"
        duration that grows on every 15s dashboard poll and that nothing
        can ever re-check or clear — a confident falsehood about a symbol
        the engine has stopped touching.
        """
        from wobblebot.config.grid import CoinGridConfig

        engine, exchange = await self._park_btc(storage)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        [row] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert row.offside is True and row.offside_since is not None

        disabled = _grid_config(
            coins={
                "BTC": CoinGridConfig(
                    enabled=False,
                    spacing_percentage=Decimal("1.0"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                )
            }
        )
        restarted = GridEngine(exchange, storage, disabled, _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage, disabled)
        assert restarted.offside_ticks(BTC_USD) == 0
        assert restarted.offside_since(BTC_USD) is None

        result = await restarted.step(BTC_USD)
        assert result.action == "skipped_disabled"
        await _emit_engine_states(restarted, [BTC_USD], storage, operator_storage)
        [after] = [r for r in await operator_storage.get_engine_states() if r.symbol == BTC_USD]
        assert after.offside is False
        assert after.offside_since is None

    async def test_an_enabled_coin_is_still_seeded(
        self, storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The control for the test above: the skip is about `enabled`, not
        a blanket refusal to restore."""
        engine, exchange = await self._park_btc(storage)
        original = engine.offside_since(BTC_USD)
        await _emit_engine_states(engine, [BTC_USD], storage, operator_storage)
        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await _restore_engine_state(restarted, [BTC_USD], operator_storage, _grid_config())
        assert restarted.offside_since(BTC_USD) == original

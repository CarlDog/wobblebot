"""ADR-046: a confirmed fill with no trade rows is pending, never final.

Replays the 2026-09-10 12:47 UTC DOGE/USD loss against the engine: the
order leaves the exchange's open set, ``get_order_status`` reports the
fill, but the tick's trade-history snapshot and the order's own trade
list do not carry the trade yet. The engine must close the order and
place its counter exactly once, write a pending marker instead of a
silent zero-trade fill, keep sweeping until the rows arrive, and page
when it gives up. The mock exchange's ``withhold_trades`` reproduces the
exchange shape; ``history_only=True`` reproduces the fast path where the
order's own trade list is ahead of the account-wide history.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.services.test_grid_engine import (
    BTC_USD,
    _exchange,
    _grid_config,
    _PartialFillOnCancelExchange,
    _safety_config,
)
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services import grid_engine as grid_engine_module
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.reconciler import resolve_fill_trades, trades_cover_fill

pytestmark = pytest.mark.unit

ENGINE_LOGGER = "wobblebot.services.grid_engine"


@pytest.fixture(autouse=True)
def _lookup_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most scenarios here are about WHAT the sweep does, not its cadence,
    so ask the exchange on every sweep tick. ``TestLookupPacing`` restores
    the production cadence explicitly and pins it."""
    monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 1)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _order(filled: str, amount: str = "1", exchange_id: str = "OX-1") -> Order:
    order = Order(
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal(amount), asset="BTC"),
        created_at=Timestamp(dt=datetime(2026, 9, 10, 12, 47, tzinfo=UTC)),
    )
    order.mark_open(exchange_id)
    return order.model_copy(update={"status": "closed", "filled_amount": Decimal(filled)})


def _trade(trade_id: str, amount: str, order_id: str = "OX-1", seconds: int = 0) -> Trade:
    qty = Decimal(amount)
    return Trade(
        id=trade_id,
        order_id=order_id,
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=qty, asset="BTC"),
        fee=Decimal("0"),
        cost=Decimal("50000") * qty,
        executed_at=Timestamp(dt=datetime(2026, 9, 10, 12, 47, 13 + seconds, tzinfo=UTC)),
    )


class _ScriptedTradesAdapter:
    """``_AdapterLike`` for resolve_fill_trades: scripted order-trade replies."""

    def __init__(self, replies: list[list[Trade] | Exception]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        return []

    async def get_order_status(self, order: Order) -> Order:
        return order

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        return []

    async def get_order_trades(self, order: Order) -> list[Trade]:
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TestTradesCoverFill:
    def test_zero_fill_is_trivially_covered(self) -> None:
        assert trades_cover_fill(_order("0"), []) is True

    def test_exact_single_trade_covers(self) -> None:
        assert trades_cover_fill(_order("1"), [_trade("T1", "1")]) is True

    def test_two_trades_summing_to_fill_cover(self) -> None:
        assert trades_cover_fill(_order("1"), [_trade("T1", "0.4"), _trade("T2", "0.6")]) is True

    def test_short_by_more_than_tolerance_does_not_cover(self) -> None:
        assert trades_cover_fill(_order("1"), [_trade("T1", "0.6")]) is False

    def test_rounding_dust_within_tolerance_covers(self) -> None:
        assert trades_cover_fill(_order("1"), [_trade("T1", "0.999999995")]) is True

    def test_empty_list_against_a_fill_does_not_cover(self) -> None:
        assert trades_cover_fill(_order("1"), []) is False


@pytest.mark.asyncio
class TestResolveFillTrades:
    async def test_covering_snapshot_skips_the_lookup(self) -> None:
        adapter = _ScriptedTradesAdapter([])
        trades, pending = await resolve_fill_trades(adapter, _order("1"), [_trade("T1", "1")])
        assert (len(trades), pending, adapter.calls) == (1, False, 0)

    async def test_short_snapshot_completed_by_the_orders_own_list(self) -> None:
        adapter = _ScriptedTradesAdapter([[_trade("T1", "0.4"), _trade("T2", "0.6", seconds=2)]])
        trades, pending = await resolve_fill_trades(adapter, _order("1"), [_trade("T1", "0.4")])
        assert [t.id for t in trades] == ["T1", "T2"]
        assert pending is False

    async def test_empty_lookup_is_pending(self) -> None:
        adapter = _ScriptedTradesAdapter([[]])
        trades, pending = await resolve_fill_trades(adapter, _order("1"), [])
        assert (trades, pending) == ((), True)

    async def test_lookup_failure_fails_soft_to_pending(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _ScriptedTradesAdapter([ExchangeError("QueryOrders: EAPI:Rate limit exceeded")])
        with caplog.at_level(logging.WARNING, logger="wobblebot.services.reconciler"):
            trades, pending = await resolve_fill_trades(adapter, _order("1"), [])
        assert (trades, pending) == ((), True)
        assert "order-trade lookup failed" in caplog.text


async def _age_marker(storage: SQLiteStorageAdapter, order: Order, *, minutes: int) -> None:
    """Backdate a marker's first_seen_at, as a long restart leaves it."""
    assert storage._conn is not None
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    await storage._conn.execute(
        "UPDATE pending_fill_trades SET first_seen_at = ? WHERE order_id = ?",
        (stamp, str(order.id)),
    )
    await storage._conn.commit()


async def _initialize_and_pick_buy(engine: GridEngine, storage: SQLiteStorageAdapter) -> Order:
    await engine.step(BTC_USD)  # anchors at 50000 and lays out 3 buys + 3 sells
    return next(
        o
        for o in await storage.get_open_orders(symbol=BTC_USD)
        if o.side is OrderSide.BUY and o.price.amount == Decimal("49500")
    )


@pytest.mark.asyncio
class TestDetectFillsWithLaggingTrades:
    async def test_replay_of_2026_09_10_closes_order_writes_marker_and_recovers(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))  # fills the 49500 buy; trade hidden

        with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
            result = await engine.step(BTC_USD)

        # The fill is detected and countered exactly once: one SELL placed,
        # nothing refused, nothing deferred (the guard has no basis yet).
        assert result.fills == 1
        assert (result.counters_placed, result.refusals, result.sells_deferred) == (1, 0, 0)
        # ...but NOT finalized: no trade row, a pending marker instead.
        assert result.trade_ids == []
        closed = await storage.get_order(buy.id)
        assert closed is not None and closed.status == "closed"
        assert closed.filled_amount == buy.amount.value
        assert await storage.get_trades(symbol=BTC_USD) == []
        markers = await storage.get_pending_fill_trades(BTC_USD)
        assert [m.exchange_id for m in markers] == [buy.exchange_id]
        assert BTC_USD in engine.pending_fill_trade_symbols()
        assert "recovery scheduled" in caplog.text
        open_after_fill = {
            (o.side, o.price.amount) for o in await storage.get_open_orders(symbol=BTC_USD)
        }
        assert (OrderSide.SELL, Decimal("50000")) in open_after_fill  # the counter

        exchange.release_trades(buy.exchange_id)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
            recovered = await engine.step(BTC_USD)

        assert recovered.trades_recovered == 1
        assert recovered.trade_recovery_abandoned == ()
        trades = await storage.get_trades(symbol=BTC_USD)
        assert [t.order_id for t in trades] == [buy.exchange_id]
        assert await storage.get_pending_fill_trades(BTC_USD) == []
        assert BTC_USD not in engine.pending_fill_trade_symbols()
        assert "fill fully recorded" in caplog.text
        # No second counter: the order was closed at detection time, so the
        # open set is exactly what it was after the first counter.
        assert {
            (o.side, o.price.amount) for o in await storage.get_open_orders(symbol=BTC_USD)
        } == open_after_fill

    async def test_fast_path_records_trades_in_the_same_tick(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id, history_only=True)
        exchange.set_price(BTC_USD, Decimal("49400"))

        with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
            result = await engine.step(BTC_USD)

        assert result.fills == 1
        assert len(result.trade_ids) == 1
        assert len(await storage.get_trades(symbol=BTC_USD)) == 1
        assert await storage.get_pending_fill_trades(BTC_USD) == []
        assert "recovery scheduled" not in caplog.text

    async def test_recovery_runs_for_a_paused_symbol(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        engine.pause_symbol(BTC_USD)
        exchange.release_trades(buy.exchange_id)

        result = await engine.step(BTC_USD)

        assert result.action == "skipped_paused"
        assert result.trades_recovered == 1
        assert len(await storage.get_trades(symbol=BTC_USD)) == 1

    async def test_abandons_after_the_attempt_bound_and_keeps_the_marker(
        self,
        storage: SQLiteStorageAdapter,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 2)
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # fill detected, marker written

        first = await engine.step(BTC_USD)  # empty lookup #1
        assert first.trade_recovery_abandoned == ()
        assert (await storage.get_pending_fill_trades(BTC_USD))[0].attempts == 1

        with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
            second = await engine.step(BTC_USD)  # empty lookup #2 hits the bound

        assert second.trade_recovery_abandoned == (buy.exchange_id,)
        assert "giving up on trade rows" in caplog.text
        assert await storage.get_pending_fill_trades(BTC_USD) == []
        kept = await storage.get_pending_fill_trades(BTC_USD, include_given_up=True)
        assert len(kept) == 1 and kept[0].given_up_at is not None and kept[0].attempts == 2
        assert BTC_USD not in engine.pending_fill_trade_symbols()
        assert await storage.get_trades(symbol=BTC_USD) == []

    async def test_transport_failure_is_not_an_attempt(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)

        class _FlakyLookup(MockExchangeAdapter):
            fail = True

            async def get_order_trades(self, order: Order) -> list[Trade]:
                if self.fail:
                    raise ExchangeError("QueryTrades: EService:Unavailable")
                return await super().get_order_trades(order)

        exchange = _FlakyLookup(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # fast path raises -> pending marker

        for _ in range(3):
            result = await engine.step(BTC_USD)
            assert result.trade_recovery_abandoned == ()
        marker = (await storage.get_pending_fill_trades(BTC_USD))[0]
        assert marker.attempts == 0

        exchange.fail = False
        exchange.release_trades(buy.exchange_id)
        recovered = await engine.step(BTC_USD)
        assert recovered.trades_recovered == 1
        assert len(await storage.get_trades(symbol=BTC_USD)) == 1

    async def test_wall_clock_ceiling_ends_the_loop_even_during_an_outage(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_AGE", timedelta(0))

        class _DeadLookup(MockExchangeAdapter):
            async def get_order_trades(self, order: Order) -> list[Trade]:
                raise ExchangeError("connection reset")

        exchange = _DeadLookup(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)

        result = await engine.step(BTC_USD)

        assert result.trade_recovery_abandoned == (buy.exchange_id,)
        assert await storage.get_pending_fill_trades(BTC_USD) == []
        assert len(await storage.get_pending_fill_trades(BTC_USD, include_given_up=True)) == 1

    async def test_partial_arrival_keeps_the_marker_until_the_fill_is_covered(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        class _PartialThenFull(MockExchangeAdapter):
            replies: list[list[Trade]] = []

            async def get_order_trades(self, order: Order) -> list[Trade]:
                if self.replies:
                    return self.replies.pop(0)
                return await super().get_order_trades(order)

        exchange = _PartialThenFull(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        half = buy.amount.value / 2
        partial = _trade("T-HALF", str(half), order_id=buy.exchange_id)
        exchange.replies = [[], [partial]]  # fast path: nothing; sweep #1: half
        await engine.step(BTC_USD)  # detection (fast path empty -> marker)

        first = await engine.step(BTC_USD)  # sweep gets half

        assert first.trades_recovered == 0
        assert [t.id for t in await storage.get_trades(symbol=BTC_USD)] == ["T-HALF"]
        marker = (await storage.get_pending_fill_trades(BTC_USD))[0]
        assert marker.attempts == 1  # progress, but still an empty-of-the-rest lookup

        exchange.release_trades(buy.exchange_id)  # the mock's real full trade now visible
        second = await engine.step(BTC_USD)

        assert second.trades_recovered == 1
        assert await storage.get_pending_fill_trades(BTC_USD) == []
        assert len(await storage.get_trades(symbol=BTC_USD)) == 2

    async def test_boot_load_resumes_a_sweep_left_by_the_previous_process(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        assert restarted.pending_fill_trade_symbols() == frozenset()
        assert await restarted.load_pending_fill_trades() == {BTC_USD: 1}
        assert restarted.pending_fill_trade_symbols() == frozenset({BTC_USD})

        exchange.release_trades(buy.exchange_id)
        result = await restarted.step(BTC_USD)

        assert result.trades_recovered == 1
        assert len(await storage.get_trades(symbol=BTC_USD)) == 1


@pytest.mark.asyncio
class TestCancelPathWithLaggingTrades:
    async def test_partial_fill_caught_at_cancel_becomes_pending_then_recovers(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _PartialFillOnCancelExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
            partial_fill_amount=Decimal("0.001"),
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        opens = await storage.get_open_orders(symbol=BTC_USD)
        for order in opens:
            assert order.exchange_id
            exchange.withhold_trades(order.exchange_id)

        cancelled, failed = await engine.cancel_open_orders(symbol=BTC_USD)

        assert (cancelled, failed) == (6, 0)
        for order in opens:
            stored = await storage.get_order(order.id)
            assert stored is not None and stored.status == "canceled"
            assert stored.filled_amount == Decimal("0.001")
            assert order.id in engine._pending_counter_ids  # pylint: disable=protected-access
        assert await storage.get_trades(symbol=BTC_USD) == []
        assert len(await storage.get_pending_fill_trades(BTC_USD)) == 6
        assert BTC_USD in engine.pending_fill_trade_symbols()

        exchange.release_trades()
        result = await engine.step(BTC_USD)

        assert result.trades_recovered == 6
        assert len(await storage.get_trades(symbol=BTC_USD)) == 6
        assert await storage.get_pending_fill_trades(BTC_USD) == []


@pytest.mark.asyncio
class TestReviewRoundPins:
    """Behaviors the 2026-09-18 review found unpinned."""

    async def test_recovery_via_the_shared_snapshot_when_the_lookup_is_dead(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        class _DeadLookup(MockExchangeAdapter):
            async def get_order_trades(self, order: Order) -> list[Trade]:
                raise ExchangeError("QueryOrders unavailable")

        exchange = _DeadLookup(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # pending marker (fast path dead)
        hidden = [  # pylint: disable=protected-access
            t for t in exchange._trade_history if t.order_id == buy.exchange_id
        ]
        assert len(hidden) == 1

        # Another symbol's fill caused a whole-account snapshot this tick;
        # it carries our row even though history for us is still withheld.
        result = await engine.step(BTC_USD, exchange_trades=hidden)

        assert result.trades_recovered == 1
        assert len(await storage.get_trades(symbol=BTC_USD)) == 1
        assert await storage.get_pending_fill_trades(BTC_USD) == []

    async def test_recovery_invalidates_the_sell_guard_cache(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        invalidated: list[Symbol] = []
        guard = engine._sell_guard  # pylint: disable=protected-access
        real = guard.invalidate

        def spy(symbol: Symbol) -> None:
            invalidated.append(symbol)
            real(symbol)

        monkeypatch.setattr(guard, "invalidate", spy)
        exchange.release_trades(buy.exchange_id)

        await engine.step(BTC_USD)

        assert invalidated == [BTC_USD]

    async def test_fee_drift_and_recovery_logs_count_each_row_once(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A partially covered fill is swept every tick; the rows already in
        storage must not be re-counted as fee anomalies or re-logged."""

        class _StuckHalf(MockExchangeAdapter):
            half: list[Trade] = []

            async def get_order_trades(self, order: Order) -> list[Trade]:
                if self.half:
                    return list(self.half)
                return await super().get_order_trades(order)

        exchange = _StuckHalf(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # half is [] -> pending
        exchange.half = [_trade("T-HALF", str(buy.amount.value / 2), order_id=buy.exchange_id)]

        with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
            await engine.step(BTC_USD)  # half arrives: recorded once
            after_first = engine.fee_anomaly_count(BTC_USD)
            first_lines = caplog.text.count("still pending")
            for _ in range(3):
                await engine.step(BTC_USD)  # same half every time
            after_repeats = engine.fee_anomaly_count(BTC_USD)
            attempts_before_release = (await storage.get_pending_fill_trades(BTC_USD))[0].attempts
            assert [t.id for t in await storage.get_trades(symbol=BTC_USD)] == ["T-HALF"]
            # Now the real (mock) trade becomes visible next to the recorded
            # half: exactly ONE new row, so exactly one more anomaly.
            exchange.half = []
            exchange.release_trades(buy.exchange_id)
            await engine.step(BTC_USD)

        assert after_first == 1  # fee 0 vs believed 0.4%/0.8% is a drift
        assert after_repeats == after_first
        assert attempts_before_release == 4
        assert engine.fee_anomaly_count(BTC_USD) == 2
        assert caplog.text.count("still pending") == first_lines == 1
        assert "fill fully recorded" in caplog.text
        assert len(await storage.get_trades(symbol=BTC_USD)) == 2
        assert await storage.get_pending_fill_trades(BTC_USD) == []

    async def test_boot_clears_a_given_up_marker_whose_rows_were_backfilled(
        self,
        storage: SQLiteStorageAdapter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        given_up = await engine.step(BTC_USD)
        assert given_up.trade_recovery_abandoned == (buy.exchange_id,)

        # Still short at boot: loud, and not counted as active.
        with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
            restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
            assert await restarted.load_pending_fill_trades() == {}
        assert "unrecovered fill on record" in caplog.text
        assert len(await storage.get_pending_fill_trades(BTC_USD, include_given_up=True)) == 1

        # Operator backfills the row by hand; the next boot clears the marker.
        hidden = [  # pylint: disable=protected-access
            t for t in exchange._trade_history if t.order_id == buy.exchange_id
        ]
        await storage.save_trade(hidden[0])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
            again = GridEngine(exchange, storage, _grid_config(), _safety_config())
            assert await again.load_pending_fill_trades() == {}
        assert "marker cleared" in caplog.text
        assert await storage.get_pending_fill_trades(BTC_USD, include_given_up=True) == []

    async def test_abandonment_is_buffered_until_drained(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The give-up write is durable and one-shot; the exchange id must
        survive in the engine until something drains it, even if the step
        that follows the sweep raises."""
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)

        class _TickerFails(MockExchangeAdapter):
            fail_ticker = False

            async def get_ticker(self, symbol: Symbol):  # type: ignore[no-untyped-def]
                if self.fail_ticker:
                    raise ExchangeError("Ticker unavailable")
                return await super().get_ticker(symbol)

        exchange = _TickerFails(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        exchange.fail_ticker = True

        with pytest.raises(ExchangeError):
            await engine.step(BTC_USD)  # sweep gives up, then the step raises

        assert engine.drain_unpaged_abandonments(BTC_USD) == (buy.exchange_id,)
        assert engine.drain_unpaged_abandonments(BTC_USD) == ()


@pytest.mark.asyncio
class TestLookupPacing:
    async def test_exchange_is_asked_every_third_sweep_tick(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 3)

        class _Counting(MockExchangeAdapter):
            lookups = 0

            async def get_order_trades(self, order: Order) -> list[Trade]:
                self.lookups += 1
                return await super().get_order_trades(order)

        exchange = _Counting(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # detection: the fast path is one lookup
        assert exchange.lookups == 1

        for _ in range(6):
            await engine.step(BTC_USD)  # sweep ticks 0..5

        assert exchange.lookups == 1 + 2  # sweep ticks 0 and 3 only
        marker = (await storage.get_pending_fill_trades(BTC_USD))[0]
        assert marker.attempts == 2  # off-cadence passes are not attempts

        exchange.release_trades(buy.exchange_id)
        recovered = 0
        for _ in range(3):
            recovered += (await engine.step(BTC_USD)).trades_recovered
        assert recovered == 1


class TestCoverageEdge:
    def test_empty_set_never_covers_a_one_lot_unit_fill(self) -> None:
        assert trades_cover_fill(_order("0.00000001"), []) is False

    @pytest.mark.asyncio
    async def test_one_lot_unit_fill_with_no_rows_asks_the_exchange(self) -> None:
        adapter = _ScriptedTradesAdapter([[_trade("T1", "0.00000001")]])
        trades, pending = await resolve_fill_trades(adapter, _order("0.00000001"), [])
        assert (len(trades), pending, adapter.calls) == (1, False, 1)


@pytest.mark.asyncio
class TestFixRoundPins:
    """2026-09-18 fix-round review (R6) and completeness critic: the second
    round's findings, each pinned by the probe that reproduced it."""

    async def test_marker_carried_past_the_ceiling_gets_a_fresh_window_after_boot(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Critic F1 / R6 finding 4: the ceiling was measured from the previous
        session's first_seen_at alone, so an inherited marker already past 30
        minutes was given up on ONE transient failure of the new session's
        first lookup (attempts=0) although Kraken had the trade."""

        class _FlakyOnce(MockExchangeAdapter):
            fail_next = False

            async def get_order_trades(self, order: Order) -> list[Trade]:
                if self.fail_next:
                    self.fail_next = False
                    raise ExchangeError("EAPI:Rate limit exceeded")
                return await super().get_order_trades(order)

        exchange = _FlakyOnce(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        first_session = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(first_session, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await first_session.step(BTC_USD)
        assert len(await storage.get_pending_fill_trades(BTC_USD)) == 1
        # The process stops; 31 minutes pass; Kraken's history catches up.
        await _age_marker(storage, buy, minutes=31)
        exchange.release_trades(buy.exchange_id)

        second_session = GridEngine(exchange, storage, _grid_config(), _safety_config())
        assert await second_session.load_pending_fill_trades() == {BTC_USD: 1}
        exchange.fail_next = True
        first_tick = await second_session.step(BTC_USD)

        assert first_tick.trade_recovery_abandoned == ()
        assert len(await storage.get_pending_fill_trades(BTC_USD)) == 1
        second_tick = await second_session.step(BTC_USD)
        assert second_tick.trades_recovered == 1
        assert await storage.get_pending_fill_trades(BTC_USD, include_given_up=True) == []

    async def test_aged_out_marker_is_given_up_on_an_off_cadence_tick(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6 escaped mutant M14: every ceiling test ran at lookup cadence 1,
        so a mutant that skipped the aged-out give-up on an off-cadence pass
        stayed green. Production cadence, ceiling zero, sweep tick seeded
        off-cadence: the single step must still abandon."""
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_LOOKUP_EVERY_TICKS", 3)
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_AGE", timedelta(0))
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)  # fill detected after this tick's sweep; marker written
        engine._sweep_ticks[BTC_USD] = 1  # the next sweep is an off-cadence pass

        result = await engine.step(BTC_USD)

        assert result.trade_recovery_abandoned == (buy.exchange_id,)
        assert await storage.get_pending_fill_trades(BTC_USD) == []

    async def test_boot_clears_a_given_up_marker_whose_order_row_is_gone(
        self, storage: SQLiteStorageAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6 finding 2: boot judged coverage only when the orders row still
        existed, so a marker whose order row was gone re-raised the ERROR on
        every boot even after the operator backfilled the trade."""
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        given_up = await engine.step(BTC_USD)
        assert given_up.trade_recovery_abandoned == (buy.exchange_id,)

        # The orders row disappears (a hand cleanup; retention never prunes
        # it) and the operator backfills the trade per the runbook.
        assert storage._conn is not None
        await storage._conn.execute("DELETE FROM orders WHERE id = ?", (str(buy.id),))
        await storage._conn.commit()
        exchange.release_trades(buy.exchange_id)
        for trade in await exchange.get_order_trades(buy):
            await storage.save_trade(trade)

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        assert await restarted.load_pending_fill_trades() == {}
        assert await storage.get_pending_fill_trades(BTC_USD, include_given_up=True) == []

    async def test_boot_re_check_ignores_rows_of_other_orders_on_the_symbol(
        self,
        storage: SQLiteStorageAdapter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fix-round reviewer, round 2 (its mutant R2-M9): the boot re-check's
        order scoping was correct but unpinned -- with the filter dropped, any
        row on the symbol at enough volume cleared the marker and the
        cost-basis gap went silent."""
        monkeypatch.setattr(grid_engine_module, "_PENDING_FILL_TRADES_MAX_ATTEMPTS", 1)
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        buy = await _initialize_and_pick_buy(engine, storage)
        assert buy.exchange_id
        exchange.withhold_trades(buy.exchange_id)
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        given_up = await engine.step(BTC_USD)
        assert given_up.trade_recovery_abandoned == (buy.exchange_id,)
        # A row for a DIFFERENT order on the same symbol, at twice the volume.
        await storage.save_trade(
            _trade("T-OTHER", str(buy.amount.value * 2), order_id="SOME-OTHER-ORDER")
        )

        restarted = GridEngine(exchange, storage, _grid_config(), _safety_config())
        with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
            assert await restarted.load_pending_fill_trades() == {}

        assert len(await storage.get_pending_fill_trades(BTC_USD, include_given_up=True)) == 1
        assert "unrecovered fill on record" in caplog.text

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

        # The fill is detected and countered exactly once...
        assert result.fills == 1
        assert result.counters_placed + result.sells_deferred + result.refusals == 1
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
        open_after_fill = len(await storage.get_open_orders(symbol=BTC_USD))

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
        # No second counter: the order was closed at detection time.
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == open_after_fill

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
        assert await restarted.load_pending_fill_trades() == 1
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

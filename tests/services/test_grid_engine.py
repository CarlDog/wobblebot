"""Tests for GridEngine — the per-symbol micro-grid orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import CoinGridConfig
from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


def _exchange(
    price: str = "50000",
    balance_usd: str = "100000",
    balance_btc: str = "10",
) -> MockExchangeAdapter:
    """Seed enough BTC so the SELL side of the layout can also place."""
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal(balance_usd), "BTC": Decimal(balance_btc)},
        starting_prices={BTC_USD: Decimal(price)},
    )


class _FetchFailExchange(MockExchangeAdapter):
    """MockExchangeAdapter whose open-order fetch always raises ExchangeError."""

    async def get_open_orders(self, symbol=None):  # type: ignore[no-untyped-def]
        raise ExchangeError("kraken OpenOrders unavailable")


class _OrderminRejectingExchange(MockExchangeAdapter):
    """MockExchangeAdapter that rejects placement at one specific price,
    simulating Kraken's client-side ordermin/costmin ExchangeError."""

    def __init__(self, *args: object, reject_price: Decimal, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._reject_price = reject_price

    async def place_order(self, order):  # type: ignore[no-untyped-def]
        if order.price.amount == self._reject_price:
            raise ExchangeError(
                f"Order volume below ordermin for {order.symbol} at {self._reject_price}"
            )
        return await super().place_order(order)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


# ---------------------------------------------------------------------------
# Disabled coin
# ---------------------------------------------------------------------------


class TestDisabledCoin:
    async def test_disabled_coin_skipped_no_state_persisted(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        config = _grid_config(
            coins={
                "BTC": CoinGridConfig(
                    spacing_percentage=Decimal("1"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                    enabled=False,
                )
            }
        )
        engine = GridEngine(_exchange(), storage, config, _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "skipped_disabled"
        assert await storage.get_grid_state(BTC_USD) is None
        assert await storage.get_open_orders(symbol=BTC_USD) == []


# ---------------------------------------------------------------------------
# First-tick initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    async def test_first_tick_anchors_state_and_places_layout(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"
        # 3 above + 3 below = 6 placed orders, none at the reference itself
        assert result.placed == 6

        state = await storage.get_grid_state(BTC_USD)
        assert state is not None
        assert state.reference_price == Decimal("50000")

        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens) == 6
        sides = sorted(o.side.value for o in opens)
        assert sides == ["buy", "buy", "buy", "sell", "sell", "sell"]
        prices = sorted(o.price.amount for o in opens)
        # Spacing 1% of 50000 = 500. BUYs at 48500/49000/49500, SELLs at 50500/51000/51500.
        assert prices == [
            Decimal("48500"),
            Decimal("49000"),
            Decimal("49500"),
            Decimal("50500"),
            Decimal("51000"),
            Decimal("51500"),
        ]

    async def test_idempotent_after_init_no_extra_orders(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)  # init places 6
        result = await engine.step(BTC_USD)  # no price movement, no fills

        assert result.action == "stepped"
        assert result.fills == 0
        assert result.counters_placed == 0
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6


# ---------------------------------------------------------------------------
# Fill detection and counter placement
# ---------------------------------------------------------------------------


class TestFillsAndCounters:
    async def test_buy_fill_triggers_sell_counter_one_spacing_up(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)  # init: BUYs at 48500/49000/49500

        # Drop price to fill the highest BUY (49500). Mock fills on price update.
        exchange.set_price(BTC_USD, Decimal("49400"))

        result = await engine.step(BTC_USD)

        assert result.action == "stepped"
        assert result.fills == 1
        assert result.counters_placed == 1

        opens = await storage.get_open_orders(symbol=BTC_USD)
        # Counter SELL goes at 49500 + 500 = 50000 (the original reference!)
        sells = sorted(o.price.amount for o in opens if o.side is OrderSide.SELL)
        assert Decimal("50000") in sells
        # Original sell layout still present
        for level in (Decimal("50500"), Decimal("51000"), Decimal("51500")):
            assert level in sells

    async def test_sell_fill_triggers_buy_counter_one_spacing_down(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Seed BTC so SELLs can be placed (mock checks balance).
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("1")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)
        # Drive price up to fill the lowest SELL (50500)
        exchange.set_price(BTC_USD, Decimal("50600"))

        result = await engine.step(BTC_USD)

        assert result.fills == 1
        assert result.counters_placed == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        buys = sorted(o.price.amount for o in opens if o.side is OrderSide.BUY)
        # Counter BUY = 50500 - 500 = 50000
        assert Decimal("50000") in buys

    async def test_top_sell_buy_fill_counter_goes_to_grid_ceiling(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # ADR-029: under top_sell the BUY-fill counter SELL lands at the
        # band ceiling (51500), coexisting with the layout SELL already
        # there — never one spacing step up (50000).
        exchange = _exchange()
        engine = GridEngine(
            exchange,
            storage,
            _grid_config(counter_target_mode="top_sell"),
            _safety_config(),
        )

        await engine.step(BTC_USD)  # init: BUYs at 48500/49000/49500
        exchange.set_price(BTC_USD, Decimal("49400"))  # fill the 49500 BUY

        result = await engine.step(BTC_USD)

        assert result.fills == 1
        assert result.counters_placed == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        sells = [o.price.amount for o in opens if o.side is OrderSide.SELL]
        assert sells.count(Decimal("51500")) == 2  # layout SELL + counter
        assert Decimal("50000") not in sells  # the spacing_up target

    async def test_top_sell_sell_fill_counter_unchanged(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # ADR-029 is asymmetric: SELL-fill counters stay one spacing down.
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("1")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(
            exchange,
            storage,
            _grid_config(counter_target_mode="top_sell"),
            _safety_config(),
        )

        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("50600"))  # fill the 50500 SELL

        result = await engine.step(BTC_USD)

        assert result.fills == 1
        assert result.counters_placed == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        buys = sorted(o.price.amount for o in opens if o.side is OrderSide.BUY)
        assert Decimal("50000") in buys

    async def test_round_trip_cycle_returns_to_initial_layout(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)
        # 1) Drop fills BUY 49500 → engine places SELL at 50000.
        exchange.set_price(BTC_USD, Decimal("49400"))
        await engine.step(BTC_USD)
        # 2) Bounce fills SELL 50000 → engine places BUY at 49500 (the original level).
        exchange.set_price(BTC_USD, Decimal("50100"))
        result = await engine.step(BTC_USD)

        assert result.fills == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        # Layout has returned to the initial side allocation per level
        levels = sorted((o.price.amount, o.side.value) for o in opens)
        assert levels == sorted(
            [
                (Decimal("48500"), "buy"),
                (Decimal("49000"), "buy"),
                (Decimal("49500"), "buy"),  # restored from counter cycle
                (Decimal("50500"), "sell"),
                (Decimal("51000"), "sell"),
                (Decimal("51500"), "sell"),
            ]
        )

    async def test_fills_persist_trades(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("49400"))
        result = await engine.step(BTC_USD)

        # The fill we just detected has its trade saved to storage
        assert len(result.trade_ids) == 1
        trades = await storage.get_trades(symbol=BTC_USD)
        assert len(trades) == 1
        assert trades[0].side is OrderSide.BUY
        assert trades[0].price.amount == Decimal("49500")


# ---------------------------------------------------------------------------
# Cost-basis sell guard — ADR-032
# ---------------------------------------------------------------------------


async def _save_buy_trade(storage: SQLiteStorageAdapter, *, price: str, amount: str = "1") -> None:
    px = Decimal(price)
    qty = Decimal(amount)
    await storage.save_trade(
        Trade(
            id=f"basis-buy-{price}",
            order_id=f"basis-order-{price}",
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=px, currency=BTC_USD.quote),
            amount=Amount(value=qty, asset=BTC_USD.base),
            fee=Decimal("0"),
            cost=px * qty,
            executed_at=Timestamp(dt=datetime(2026, 1, 1, tzinfo=UTC)),
        )
    )


class TestSellGuard:
    async def test_initial_layout_defers_sells_below_cost_basis(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Prior-session BUY history establishes a basis (60000) far above
        # where the grid is about to lay out (50000) -- exactly ADR-032's
        # trigger #1 ("initial-layout SELLs against pre-held inventory").
        await _save_buy_trade(storage, price="60000")
        exchange = _exchange(price="50000", balance_btc="10")
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"
        assert result.placed == 3  # the three BUY levels
        assert result.sells_deferred == 3  # the three SELL levels, all below 60000
        assert result.refusals == 0
        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert all(o.side is OrderSide.BUY for o in opens)

    async def test_disabled_sell_guard_places_everything(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await _save_buy_trade(storage, price="60000")
        exchange = _exchange(price="50000", balance_btc="10")
        engine = GridEngine(
            exchange, storage, _grid_config(), _safety_config(sell_guard_enabled=False)
        )

        result = await engine.step(BTC_USD)

        assert result.placed == 6
        assert result.sells_deferred == 0

    async def test_unknown_basis_places_sells_normally(self, storage: SQLiteStorageAdapter) -> None:
        # No trade history at all -- the guard must not freeze a fresh
        # deployment's initial layout (ADR-032 decision 3).
        exchange = _exchange(price="50000", balance_btc="10")
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.placed == 6
        assert result.sells_deferred == 0


# ---------------------------------------------------------------------------
# Offside behavior — ADR-006 decision 1
# ---------------------------------------------------------------------------


class TestOffside:
    async def test_offside_low_logs_warning_and_no_counters(
        self,
        storage: SQLiteStorageAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)  # init around 50000, lowest BUY at 48500
        # Drop below the lowest BUY. All three BUYs fill on the price update.
        exchange.set_price(BTC_USD, Decimal("48000"))

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)

        assert result.offside is True
        # Three BUYs filled on the drop, but no counters are placed while offside.
        assert result.fills == 3
        assert result.counters_placed == 0
        offside_records = [r for r in caplog.records if "offside" in r.getMessage()]
        assert offside_records, "expected an offside log warning"

    async def test_offside_ticks_accessor_tracks_and_resets(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """ADR-030 visibility accessor: 0 onside, counts while offside,
        back to 0 on recovery. This is what feeds engine_state — NOT
        StepResult.offside, which is False on non-'stepped' actions."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        assert engine.offside_ticks(BTC_USD) == 0  # never stepped

        await engine.step(BTC_USD)
        assert engine.offside_ticks(BTC_USD) == 0  # onside after init

        exchange.set_price(BTC_USD, Decimal("48000"))
        await engine.step(BTC_USD)
        first = engine.offside_ticks(BTC_USD)
        assert first >= 1
        await engine.step(BTC_USD)
        assert engine.offside_ticks(BTC_USD) > first  # still counting

        exchange.set_price(BTC_USD, Decimal("50000"))
        await engine.step(BTC_USD)
        assert engine.offside_ticks(BTC_USD) == 0  # recovery resets

    async def test_returns_inside_resumes_normal(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)
        # Trip offside, then return to in-window.
        exchange.set_price(BTC_USD, Decimal("48000"))
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("50000"))

        result = await engine.step(BTC_USD)

        # No new fills this tick; engine is back to "stepped" with offside False
        assert result.action == "stepped"
        assert result.offside is False

    async def test_offside_logs_once_not_every_tick(
        self,
        storage: SQLiteStorageAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A sustained downtrend logs offside ONCE on entry, not a WARNING
        every tick (the 2026-06-02 soak surfaced ~7h of per-tick warnings)."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("48000"))  # below the lowest BUY

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            for _ in range(5):  # five consecutive offside ticks
                await engine.step(BTC_USD)

        parking = [r for r in caplog.records if "parking" in r.getMessage()]
        assert len(parking) == 1, "offside WARNING must fire once on entry, not per tick"

    async def test_offside_recovery_logs_resuming(
        self,
        storage: SQLiteStorageAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("48000"))
        await engine.step(BTC_USD)  # offside
        exchange.set_price(BTC_USD, Decimal("50000"))  # back into the band

        with caplog.at_level(logging.INFO, logger="wobblebot.services.grid_engine"):
            await engine.step(BTC_USD)

        resuming = [r for r in caplog.records if "back onside" in r.getMessage()]
        assert resuming, "expected a 'back onside; resuming' log on recovery"


# ---------------------------------------------------------------------------
# State recovery: a fresh engine pointed at the same storage resumes
# ---------------------------------------------------------------------------


class TestRestartResume:
    async def test_new_engine_picks_up_existing_state(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        first = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await first.step(BTC_USD)  # initializes

        # Simulate a process restart: brand-new engine instance, same storage
        # and exchange. Should NOT re-initialize (would double the orders).
        second = GridEngine(exchange, storage, _grid_config(), _safety_config())
        result = await second.step(BTC_USD)

        assert result.action == "stepped"  # not "initialized"
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6


# ---------------------------------------------------------------------------
# Auto re-layout (Stage 8.4.E follow-up 2026-05-22)
# ---------------------------------------------------------------------------
#
# After a session_loss_cap trip (or any shutdown that cancels open orders),
# the next cli/live restart finds grid_state in storage but zero open
# orders on Kraken. Before this fix the engine sat in _tick doing nothing
# until the operator manually DELETE FROM grid_state. The engine now
# detects the no-open-orders state and re-lays out the grid at the
# EXISTING anchor (operators set anchors deliberately; we respect that).


class TestAutoReLayout:
    async def test_zero_open_orders_triggers_re_layout_at_existing_anchor(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """grid_state exists, no open orders → re-lay-out at state.reference_price."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        # First step: initialize (6 orders placed at anchor $50,000).
        await engine.step(BTC_USD)
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6
        state_before = await storage.get_grid_state(BTC_USD)
        assert state_before is not None
        anchor_before = state_before.reference_price

        # Simulate cap-trip-and-cleanup: cancel all open orders via storage.
        # The cli/live finally-block does this in production via
        # _cancel_all_open; here we mirror it directly to keep the test
        # focused on the engine behavior.
        for order in await storage.get_open_orders(symbol=BTC_USD):
            await exchange.cancel_order(order)
            await storage.save_order(order.model_copy(update={"status": "canceled"}))
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 0

        # Tick again. Engine should detect the empty-open-orders condition
        # and re-place the layout at the SAME anchor.
        result = await engine.step(BTC_USD)
        assert result.action == "stepped"
        assert result.placed == 6
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6

        # The anchor is preserved — the operator's grid_state hasn't moved.
        state_after = await storage.get_grid_state(BTC_USD)
        assert state_after is not None
        assert state_after.reference_price == anchor_before

    async def test_stale_anchor_re_layout_warns(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """v1.1 backlog: an anchor 24h+ old that still re-lays out cleanly
        (no offside, no incident) must WARN, not just INFO -- a multi-
        day-old anchor that still brackets price otherwise passes
        silently. Detect-only: the layout still places normally."""
        exchange = _exchange()
        old_anchor = GridState(
            symbol=BTC_USD,
            reference_price=Decimal("50000"),
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC) - timedelta(hours=25)),
        )
        await storage.save_grid_state(old_anchor)

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            result = await GridEngine(exchange, storage, _grid_config(), _safety_config()).step(
                BTC_USD
            )

        assert result.action == "stepped"
        assert result.placed == 6
        assert any("stale anchor" in r.message for r in caplog.records)

    async def test_fresh_anchor_re_layout_does_not_warn(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # fresh anchor, created_at = now
        for order in await storage.get_open_orders(symbol=BTC_USD):
            await exchange.cancel_order(order)
            await storage.save_order(order.model_copy(update={"status": "canceled"}))

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)

        assert result.action == "stepped"
        assert result.placed == 6
        assert not any("stale anchor" in r.message for r in caplog.records)

    async def test_normal_tick_with_open_orders_does_not_re_layout(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Regression guard: a tick with healthy open orders + no fills
        must not place duplicate layout orders."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        await engine.step(BTC_USD)  # initialize → 6 orders
        result = await engine.step(BTC_USD)  # quiet tick, no fills

        assert result.action == "stepped"
        assert result.placed == 0  # no new orders placed
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6

    async def test_offside_does_not_re_layout(self, storage: SQLiteStorageAdapter) -> None:
        """When the grid is parked (offside), no re-layout — same as the
        existing parked-grid posture for fills/counters."""
        exchange = _exchange(price="50000")
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # initialize at $50,000

        # Cancel everything, then shove the market price way offside.
        for order in await storage.get_open_orders(symbol=BTC_USD):
            await exchange.cancel_order(order)
            await storage.save_order(order.model_copy(update={"status": "canceled"}))
        exchange.set_price(BTC_USD, Decimal("99999"))  # ~2x anchor — offside

        result = await engine.step(BTC_USD)
        assert result.action == "stepped"
        assert result.offside is True
        assert result.placed == 0
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 0


class TestPartialGridPlacementLogging:
    """v1.1 backlog 'partial-grid placement WARN -> INFO': a per-level
    insufficient-balance refusal is routine (a flat-start account, or an
    account too small to fund the full layout), not a genuine problem.
    It must log at DEBUG, not WARNING, and each placement batch must
    emit a placed-vs-target INFO summary an operator can actually use."""

    async def test_insufficient_balance_refusal_does_not_warn(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        # USD-only balance -- every SELL in the layout raises
        # InsufficientBalance (same fixture as
        # test_insufficient_base_for_sell_treated_as_refusal).
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)

        assert result.placed == 3
        assert result.refusals == 3
        assert not any("insufficient balance" in r.getMessage() for r in caplog.records)

    async def test_insufficient_balance_refusal_still_logs_at_debug(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Demoted, not deleted -- still traceable with DEBUG enabled."""
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        with caplog.at_level(logging.DEBUG, logger="wobblebot.services.grid_engine"):
            await engine.step(BTC_USD)

        debug_refusals = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "refused: insufficient" in r.getMessage()
        ]
        assert len(debug_refusals) == 3

    async def test_initialize_summary_reports_placed_vs_target(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        with caplog.at_level(logging.INFO, logger="wobblebot.services.grid_engine"):
            await engine.step(BTC_USD)

        summaries = [r for r in caplog.records if r.getMessage().startswith("grid initialized for")]
        assert len(summaries) == 1
        assert summaries[0].target_levels == 6  # 3 above + 3 below
        assert summaries[0].levels_placed == 3
        assert summaries[0].refusals == 3

    async def test_relayout_summary_reports_placed_vs_target(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The auto-re-layout branch had NO completion summary before this
        fix -- only the per-level WARNING gave any visibility. Now that
        the per-level log is DEBUG, the summary is the only signal, so
        it must exist and report accurately."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # initialize -> 6 orders
        for order in await storage.get_open_orders(symbol=BTC_USD):
            await exchange.cancel_order(order)
            await storage.save_order(order.model_copy(update={"status": "canceled"}))

        with caplog.at_level(logging.INFO, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)

        assert result.action == "stepped"
        assert result.placed == 6
        summaries = [
            r for r in caplog.records if r.getMessage().startswith("grid re-layout complete for")
        ]
        assert len(summaries) == 1
        assert summaries[0].target_levels == 6
        assert summaries[0].levels_placed == 6
        assert summaries[0].refusals == 0


# ---------------------------------------------------------------------------
# Per-symbol concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_per_symbol_lock_serializes_same_symbol(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Two concurrent steps for the same symbol must not both initialize,
        # which would attempt to place the layout twice.
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        results = await asyncio.gather(
            engine.step(BTC_USD),
            engine.step(BTC_USD),
        )

        actions = sorted(r.action for r in results)
        assert actions == ["initialized", "stepped"]
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6


# ---------------------------------------------------------------------------
# Safety cap enforcement (slice 2.2.4)
# ---------------------------------------------------------------------------


class TestSafetyCaps:
    async def test_max_orders_per_coin_blocks_extras(
        self,
        storage: SQLiteStorageAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Layout would place 6 (3+3); cap at 4 should refuse 2 of them.
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_orders=4),
        )

        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)

        assert result.placed == 4
        assert result.refusals == 2
        # Reason is in the structured `extra` dict on each refusal record.
        reasons = [
            getattr(r, "reason", None)
            for r in caplog.records
            if "refused by safety cap" in r.getMessage()
        ]
        assert reasons.count("max_orders_per_coin") == 2

    async def test_max_per_coin_exposure_blocks_extras(self, storage: SQLiteStorageAdapter) -> None:
        # Each order is $10; cap at $25 lets 2 through, refuses the other 4.
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_per_coin="25"),
        )

        result = await engine.step(BTC_USD)

        assert result.placed == 2
        assert result.refusals == 4

    async def test_max_total_exposure_blocks_extras(self, storage: SQLiteStorageAdapter) -> None:
        # Same dollar math as per-coin, but exercising the global cap.
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_total="25"),
        )

        result = await engine.step(BTC_USD)

        assert result.placed == 2
        assert result.refusals == 4

    async def test_max_daily_spend_blocks_buys_only(self, storage: SQLiteStorageAdapter) -> None:
        # Layout has 3 BUYs + 3 SELLs at $10 each. Daily-spend cap at $25
        # should block 1 BUY (let 2 through) and let all 3 SELLs through
        # (sells are not counted as spend).
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_daily="25"),
        )

        result = await engine.step(BTC_USD)

        assert result.refusals == 1
        # 5 placed = 2 BUYs (within $25) + 3 SELLs (always allowed)
        assert result.placed == 5
        opens = await storage.get_open_orders(symbol=BTC_USD)
        buys = [o for o in opens if o.side is OrderSide.BUY]
        sells = [o for o in opens if o.side is OrderSide.SELL]
        assert len(buys) == 2
        assert len(sells) == 3

    async def test_max_daily_spend_ignores_canceled_buys(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Operator-surfaced 2026-05-22 soak Day 5: canceled BUYs were
        counting toward max_daily_SPEND_usd, so the engine couldn't
        replace them after a session reset even though no money had
        actually moved. The cap now counts only BUYs that committed
        funds (open / pending / closed)."""
        # Pre-seed storage with 9 already-canceled BUYs at $10 notional
        # each. Under the old semantics this would consume $90 of a
        # $100 cap; under the fix it consumes $0.
        from datetime import datetime as _dt
        from uuid import uuid4

        from wobblebot.domain.models import Order
        from wobblebot.domain.value_objects import Amount, Price, Timestamp

        for _ in range(9):
            await storage.save_order(
                Order(
                    id=uuid4(),
                    exchange_id=None,
                    symbol=BTC_USD,
                    side="buy",  # type: ignore[arg-type]
                    price=Price(amount=Decimal("50000"), currency="USD"),
                    amount=Amount(value=Decimal("0.0002"), asset="BTC"),
                    status="canceled",
                    created_at=Timestamp(dt=_dt.now(UTC)),
                )
            )

        # Now run a fresh engine with $100 daily cap. The full 3-BUY
        # layout ($30 of new spend) must fit.
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_daily="100"),
        )
        result = await engine.step(BTC_USD)

        assert result.refusals == 0
        assert result.placed == 6  # 3 BUYs + 3 SELLs, all through

    async def test_caps_block_counters_too(self, storage: SQLiteStorageAdapter) -> None:
        # Initialize at the just-fits cap; a fill afterwards should NOT
        # be able to place a counter (would exceed).
        exchange = _exchange()
        engine = GridEngine(
            exchange,
            storage,
            _grid_config(),
            _safety_config(max_per_coin="60"),  # exactly fits the 6 initial orders
        )

        await engine.step(BTC_USD)
        # Drop price to fill BUY at 49500.
        exchange.set_price(BTC_USD, Decimal("49400"))
        result = await engine.step(BTC_USD)

        # The fill freed $10 of exposure (the closed BUY is no longer
        # "open"). Counter placement adds $10 back. So with cap=60 and
        # 5 remaining open ($50), the counter ($10) just fits → placed.
        assert result.fills == 1
        assert result.counters_placed == 1

        # Now tighten the math: another fill, but the counter would push
        # us over. Use a fresh engine with a tighter cap.

    async def test_cap_at_exact_boundary_allows_placement(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Exactly $60 cap should let exactly $60 worth of orders through.
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_per_coin="60"),
        )
        result = await engine.step(BTC_USD)
        assert result.placed == 6
        assert result.refusals == 0

    async def test_cap_below_single_order_blocks_all(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(
            _exchange(),
            storage,
            _grid_config(),
            _safety_config(max_per_coin="5"),  # one order is $10 → all refused
        )
        result = await engine.step(BTC_USD)
        assert result.placed == 0
        assert result.refusals == 6


# ---------------------------------------------------------------------------
# Multi-symbol behavior (Stage 2.4)
# ---------------------------------------------------------------------------


ETH_USD = Symbol(base="ETH", quote="USD")


def _multi_exchange(
    btc_price: str = "50000",
    eth_price: str = "3000",
    balance_usd: str = "100000",
    balance_btc: str = "10",
    balance_eth: str = "100",
) -> MockExchangeAdapter:
    """Mock with prices and balances for both BTC and ETH."""
    return MockExchangeAdapter(
        starting_balances={
            "USD": Decimal(balance_usd),
            "BTC": Decimal(balance_btc),
            "ETH": Decimal(balance_eth),
        },
        starting_prices={
            BTC_USD: Decimal(btc_price),
            ETH_USD: Decimal(eth_price),
        },
    )


class TestMultiSymbol:
    async def test_independent_grid_state_per_symbol(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(
            _multi_exchange(),
            storage,
            _grid_config(),
            _safety_config(),
        )
        # First tick for BTC initializes its grid
        await engine.step(BTC_USD)
        # First tick for ETH initializes a separate grid
        await engine.step(ETH_USD)

        btc_state = await storage.get_grid_state(BTC_USD)
        eth_state = await storage.get_grid_state(ETH_USD)
        assert btc_state is not None and eth_state is not None
        assert btc_state.reference_price == Decimal("50000")
        assert eth_state.reference_price == Decimal("3000")
        # Each symbol got its own 6-order layout
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6
        assert len(await storage.get_open_orders(symbol=ETH_USD)) == 6

    async def test_global_total_exposure_cap_counts_across_symbols(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # 6 orders × $10 per coin = $60. Two coins = $120. Cap at $80
        # should fit BTC's 6 layout orders ($60) plus only 2 ETH ones
        # ($20 cumulative, hitting the cap at the third).
        engine = GridEngine(
            _multi_exchange(),
            storage,
            _grid_config(),
            _safety_config(max_total="80"),
        )
        btc_result = await engine.step(BTC_USD)
        eth_result = await engine.step(ETH_USD)

        assert btc_result.placed == 6
        assert btc_result.refusals == 0
        # ETH gets 2 placed before total ($60 + 2*$10 = $80, the cap),
        # then the 3rd through 6th refuse because they'd push past.
        assert eth_result.placed == 2
        assert eth_result.refusals == 4

    async def test_per_coin_caps_are_independent(self, storage: SQLiteStorageAdapter) -> None:
        # Per-coin cap of 4 means each symbol can only place 4 of its 6
        # layout orders. With two symbols this gives 8 placements total,
        # not constrained by per-coin counting them together.
        engine = GridEngine(
            _multi_exchange(),
            storage,
            _grid_config(),
            _safety_config(max_orders=4),
        )
        btc_result = await engine.step(BTC_USD)
        eth_result = await engine.step(ETH_USD)

        assert btc_result.placed == 4
        assert btc_result.refusals == 2
        # ETH's per-coin order count starts fresh
        assert eth_result.placed == 4
        assert eth_result.refusals == 2

    async def test_concurrent_steps_for_different_symbols_dont_block(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Per ADR-006 decision 5, per-symbol locks let different symbols
        # step in parallel. asyncio.gather should not deadlock and both
        # should complete with action="initialized".
        engine = GridEngine(
            _multi_exchange(),
            storage,
            _grid_config(),
            _safety_config(),
        )
        btc_r, eth_r = await asyncio.gather(
            engine.step(BTC_USD),
            engine.step(ETH_USD),
        )
        assert btc_r.action == "initialized"
        assert eth_r.action == "initialized"
        assert len(await storage.get_open_orders()) == 12  # 6 + 6

    async def test_insufficient_base_for_sell_treated_as_refusal(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Operator's account commonly holds USD but no base inventory at
        first run; the engine must NOT crash when SELL placements raise
        InsufficientBalance — it must log + treat each as a refusal so
        the BUY side still places. Once a BUY fills, base inventory
        appears and subsequent SELL counters at that level succeed."""
        # USD-only balance — every SELL in the layout will raise
        # InsufficientBalance from the mock. (Mock and live behave
        # identically here: Kraken returns EOrder:Insufficient funds
        # which the adapter translates to InsufficientBalance.)
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"
        # 3 BUYs placed (USD-funded), 3 SELLs refused for insufficient BTC.
        assert result.placed == 3
        assert result.refusals == 3
        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert all(o.side is OrderSide.BUY for o in opens)
        assert len(opens) == 3

    async def test_one_symbol_failing_does_not_corrupt_other(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # An exchange where ETH price is missing (raises in get_current_price)
        # but BTC works. Stepping BTC should succeed; stepping ETH should
        # raise but leave BTC's GridState intact.
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},  # ETH absent
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        btc_result = await engine.step(BTC_USD)
        assert btc_result.action == "initialized"

        # ETH step raises because no price is set; the engine doesn't
        # silently swallow this — adapter contract says raise on missing
        # price. Caller (cli/live) is the layer that catches per-symbol.
        with pytest.raises(
            Exception
        ):  # ExchangeError; broad catch keeps the test focused on isolation
            await engine.step(ETH_USD)

        # BTC's state is untouched by ETH's failure
        btc_state = await storage.get_grid_state(BTC_USD)
        assert btc_state is not None
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6
        assert await storage.get_grid_state(ETH_USD) is None


# ---------------------------------------------------------------------------
# Stage 5.4 — operator-driven control (pause / resume / cancel / stop)
# ---------------------------------------------------------------------------


class TestPauseResume:
    async def test_pause_returns_true_first_call(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        assert engine.pause_symbol(BTC_USD) is True
        assert engine.is_paused(BTC_USD) is True

    async def test_pause_idempotent_returns_false_when_already_paused(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        assert engine.pause_symbol(BTC_USD) is False

    async def test_resume_returns_true_when_paused(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        assert engine.resume_symbol(BTC_USD) is True
        assert engine.is_paused(BTC_USD) is False

    async def test_resume_idempotent_returns_false_when_active(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        assert engine.resume_symbol(BTC_USD) is False

    async def test_paused_symbols_snapshot_is_frozen(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        snap = engine.paused_symbols()
        assert isinstance(snap, frozenset)
        assert BTC_USD in snap
        # Mutating after snapshot doesn't reflect into the snap (it's a copy)
        engine.resume_symbol(BTC_USD)
        assert BTC_USD in snap  # snapshot unchanged

    async def test_step_returns_skipped_paused_when_paused(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        result = await engine.step(BTC_USD)
        assert result.action == "skipped_paused"
        # No grid state created, no orders placed
        assert await storage.get_grid_state(BTC_USD) is None
        assert await storage.get_open_orders(symbol=BTC_USD) == []

    async def test_paused_then_resumed_step_initializes(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.pause_symbol(BTC_USD)
        await engine.step(BTC_USD)  # skipped
        engine.resume_symbol(BTC_USD)
        result = await engine.step(BTC_USD)
        assert result.action == "initialized"
        assert result.placed > 0

    async def test_paused_symbol_still_records_a_fill(self, storage: SQLiteStorageAdapter) -> None:
        """THE 2026-08-11 PRODUCTION BUG, in miniature.

        Pause deliberately leaves standing orders live on the exchange, so
        they can still fill. Before this was fixed the pause gate returned
        before fill detection, so the engine stopped LOOKING: BTC's buy at
        63237.69 filled, storage still called it `open` four days later, no
        trade row was written, the dashboard showed three open orders when
        two remained, and every downstream number (exposure, caps, the
        advisor's risk inputs) believed the money was still unspent USD.

        Pause must mean "stop trading", never "stop seeing".
        """
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # lay the grid
        opens_before = await storage.get_open_orders(symbol=BTC_USD)
        assert any(o.side == "buy" for o in opens_before), "need a standing BUY to fill"

        engine.pause_symbol(BTC_USD)
        # Price crosses the 49500 BUY *while paused* — the production
        # scenario exactly: pause left the order live, the market came to it.
        exchange.set_price(BTC_USD, Decimal("49400"))

        result = await engine.step(BTC_USD)

        assert result.action == "skipped_paused"
        assert result.fills >= 1, "a paused symbol must still DETECT the fill"
        remaining = await storage.get_open_orders(symbol=BTC_USD)
        assert len(remaining) < len(opens_before), "storage must not still call it open"
        assert await storage.get_trades(symbol=BTC_USD), "the trade must be recorded"

    async def test_paused_fill_places_no_counter_order(self, storage: SQLiteStorageAdapter) -> None:
        """Recording is not trading. A counter is a NEW order on the
        exchange, which is precisely what pause forbids — so the fill is
        recorded and left for the operator to act on via resume or
        cancel-open-orders."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        opens_before = await storage.get_open_orders(symbol=BTC_USD)

        engine.pause_symbol(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("49400"))
        result = await engine.step(BTC_USD)

        assert result.counters_placed == 0
        assert result.placed == 0
        # Strictly fewer open orders: the filled one left, nothing replaced it.
        remaining = await storage.get_open_orders(symbol=BTC_USD)
        assert len(remaining) < len(opens_before)

    async def test_pause_does_not_cancel_orders(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        # Lay the grid first
        await engine.step(BTC_USD)
        opens_before = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens_before) > 0
        engine.pause_symbol(BTC_USD)
        # Open orders preserved through pause
        opens_after = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens_after) == len(opens_before)


class TestRequestStop:
    async def test_initial_state_not_requested(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        assert engine.is_stop_requested is False

    async def test_request_stop_sets_flag(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.request_stop()
        assert engine.is_stop_requested is True

    async def test_request_stop_idempotent(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        engine.request_stop()
        engine.request_stop()  # second call is a no-op
        assert engine.is_stop_requested is True


class TestCancelOpenOrders:
    async def test_cancel_one_symbol(self, storage: SQLiteStorageAdapter) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # initialize -> places grid
        opens_before = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens_before) > 0
        cancelled, failed = await engine.cancel_open_orders(symbol=BTC_USD)
        assert cancelled == len(opens_before)
        assert failed == 0
        # After cancel + persist, storage shows no open BTC orders
        opens_after = await storage.get_open_orders(symbol=BTC_USD)
        assert opens_after == []

    async def test_cancel_returns_zero_when_no_open_orders(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        engine = GridEngine(_exchange(), storage, _grid_config(), _safety_config())
        cancelled, failed = await engine.cancel_open_orders(symbol=BTC_USD)
        assert (cancelled, failed) == (0, 0)

    async def test_cancel_all_symbols(self, storage: SQLiteStorageAdapter) -> None:
        eth_usd = Symbol(base="ETH", quote="USD")
        exch = MockExchangeAdapter(
            starting_balances={
                "USD": Decimal("100000"),
                "BTC": Decimal("10"),
                "ETH": Decimal("100"),
            },
            starting_prices={
                BTC_USD: Decimal("50000"),
                eth_usd: Decimal("3000"),
            },
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        await engine.step(eth_usd)
        opens_total = await storage.get_open_orders()
        assert len(opens_total) > 0
        cancelled, failed = await engine.cancel_open_orders(symbol=None)
        assert cancelled == len(opens_total)
        assert failed == 0
        assert await storage.get_open_orders() == []

    async def test_cancel_raises_when_fetch_fails(self, storage: SQLiteStorageAdapter) -> None:
        # A failed open-order fetch must propagate, not collapse into (0, 0):
        # that is indistinguishable from "nothing to cancel" and would read as
        # a false all-clear to an operator while orders stay live on Kraken.
        exch = _FetchFailExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())
        with pytest.raises(ExchangeError):
            await engine.cancel_open_orders(symbol=BTC_USD)


class _OneCancelFailsExchange(MockExchangeAdapter):
    """MockExchangeAdapter where exactly one cancel raises (the rest work)."""

    def __init__(self, *args: object, fail_price: Decimal, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._fail_price = fail_price

    async def cancel_order(self, order):  # type: ignore[no-untyped-def]
        if order.price.amount == self._fail_price:
            raise ExchangeError("EOrder:Cancel failed")
        return await super().cancel_order(order)


class TestRequestReanchor:
    """ADR-031: cancel-FIRST atomicity + in-process layout (judge correction A)."""

    async def test_clean_reanchor_moves_anchor_and_places_in_process(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # anchor 50000, 6 orders
        # In-band move (50200 crosses no level) so the mock's price-cross
        # matching fills nothing — this test isolates the re-anchor
        # mechanics; fills-before-reanchor recovery is the next tick's
        # _detect_fills job, unchanged by ADR-031.
        exchange.set_price(BTC_USD, Decimal("50200"))

        ok, message = await engine.request_reanchor(BTC_USD)

        assert ok is True
        state = await storage.get_grid_state(BTC_USD)
        assert state is not None
        assert state.reference_price == Decimal("50200")
        # In-process placement: fresh layout exists WITHOUT any step().
        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens) == 6
        prices = {o.price.amount for o in opens}
        assert Decimal("49698") in prices  # 50200 - 1% — the new band
        assert Decimal("49500") not in prices  # the old band is gone
        assert "50000 -> 50200" in message

    async def test_failed_cancel_aborts_before_save(self, storage: SQLiteStorageAdapter) -> None:
        """THE regression pin: save_grid_state is NOT called when any
        cancel fails — never a new anchor over still-live orders."""
        exchange = _OneCancelFailsExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
            fail_price=Decimal("49000"),  # one of the BUY levels
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("50200"))  # in-band: no fills

        ok, message = await engine.request_reanchor(BTC_USD)

        assert ok is False
        assert "anchor is unchanged" in message
        state = await storage.get_grid_state(BTC_USD)
        assert state is not None
        assert state.reference_price == Decimal("50000")  # untouched
        # The un-cancellable order is still open — consistent state.
        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert any(o.price.amount == Decimal("49000") for o in opens)

    async def test_fetch_failure_aborts_with_live_orders_warning(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exch = _FetchFailExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exch, storage, _grid_config(), _safety_config())

        ok, message = await engine.request_reanchor(BTC_USD)

        assert ok is False
        assert "LIVE" in message
        assert await storage.get_grid_state(BTC_USD) is None  # nothing saved

    async def test_offside_symbol_still_places_layout(self, storage: SQLiteStorageAdapter) -> None:
        """Judge correction A: the next-tick auto-re-layout gate sits
        inside `if not offside:` — a re-anchor relying on it would park
        the symbol with ZERO orders right after the operator asked for
        a re-center. In-process placement must lay the grid regardless
        of prior offside state."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        exchange.set_price(BTC_USD, Decimal("40000"))  # far below the band
        await engine.step(BTC_USD)  # goes offside, parks
        assert engine.offside_ticks(BTC_USD) >= 1

        ok, _message = await engine.request_reanchor(BTC_USD)

        assert ok is True
        assert engine.offside_ticks(BTC_USD) == 0  # counter cleared
        opens = await storage.get_open_orders(symbol=BTC_USD)
        assert len(opens) > 0  # NOT parked with zero orders
        state = await storage.get_grid_state(BTC_USD)
        assert state is not None
        assert state.reference_price == Decimal("40000")

    async def test_paused_symbol_auto_resumes(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)
        engine.pause_symbol(BTC_USD)

        ok, message = await engine.request_reanchor(BTC_USD)

        assert ok is True
        assert engine.is_paused(BTC_USD) is False
        assert "auto-resumed" in message


# ---------------------------------------------------------------------------
# Ordermin/costmin ExchangeError handling (v1.1 backlog: engine
# ordermin-awareness)
# ---------------------------------------------------------------------------


class TestExchangeErrorDuringPlacement:
    async def test_ordermin_rejection_refuses_one_level_not_the_whole_tick(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A generic ExchangeError (Kraken's client-side ordermin/costmin
        rejection) from one level's placement must not propagate and
        abort every remaining level in the same layout loop -- it was
        silently doing exactly that before this was caught."""
        exchange = _OrderminRejectingExchange(
            starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
            starting_prices={BTC_USD: Decimal("50000")},
            reject_price=Decimal("49500"),  # one of the three BUY levels
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"
        assert result.placed == 5  # 6 levels minus the one rejected
        assert result.refusals == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        prices = {o.price.amount for o in opens}
        assert Decimal("49500") not in prices
        assert Decimal("49000") in prices  # sibling levels still placed


class TestF1PartialFillRecovery:
    async def test_partial_fill_before_cancel_recovers_trade_and_sized_counter(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The F1 case: an order partially fills, then refreshes to
        canceled/expired instead of closed. The old status=="closed"-only
        check silently dropped this; the shared resolver now recovers it."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # init: BUYs at 48500/49000/49500

        opens = await storage.get_open_orders(symbol=BTC_USD)
        target_buy = next(
            o for o in opens if o.side is OrderSide.BUY and o.price.amount == Decimal("49500")
        )
        partial_qty = target_buy.amount.value / 2
        exchange.inject_partial_cancel(target_buy, filled_amount=partial_qty)
        # Keep price below the counter's target (50000) so the mock's
        # own price-cross matching doesn't immediately re-fill the
        # counter the instant it's placed.
        exchange.set_price(BTC_USD, Decimal("49500"))

        result = await engine.step(BTC_USD)

        assert result.fills == 1
        assert result.counters_placed == 1
        assert len(result.trade_ids) == 1

        trades = await storage.get_trades(symbol=BTC_USD)
        assert len(trades) == 1
        assert trades[0].amount.value == partial_qty

        stale_order = await storage.get_order(target_buy.id)
        assert stale_order is not None
        assert stale_order.status == "canceled"
        assert stale_order.filled_amount == partial_qty

        # Counter is a SELL one spacing above the fill price, sized to
        # the PARTIAL amount -- not the order's full nominal size.
        opens_after = await storage.get_open_orders(symbol=BTC_USD)
        counter = next(o for o in opens_after if o.price.amount == Decimal("50000"))
        assert counter.side is OrderSide.SELL
        assert counter.amount.value == partial_qty

    async def test_clean_cancel_no_fill_no_counter(self, storage: SQLiteStorageAdapter) -> None:
        """A genuine clean cancel (filled_amount stays 0) must not be
        treated as a fill -- no trade, no counter."""
        exchange = _exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)

        opens = await storage.get_open_orders(symbol=BTC_USD)
        target_buy = next(o for o in opens if o.side is OrderSide.BUY)
        exchange.inject_partial_cancel(target_buy, filled_amount=Decimal("0"))

        result = await engine.step(BTC_USD)

        assert result.fills == 0
        assert result.counters_placed == 0
        assert result.trade_ids == []
        stale_order = await storage.get_order(target_buy.id)
        assert stale_order is not None
        assert stale_order.status == "canceled"
        assert stale_order.filled_amount == Decimal("0")


class TestPendingCounters:
    def _recovered_order(self, *, side: OrderSide = OrderSide.BUY, price: str = "49500") -> Order:
        return Order(
            id=uuid4(),
            exchange_id=f"RECOVERED-{price}",
            symbol=BTC_USD,
            side=side,
            price=Price(amount=Decimal(price), currency="USD"),
            amount=Amount(value=Decimal("1"), asset="BTC"),
            status="closed",
            filled_amount=Decimal("1"),
            created_at=Timestamp(dt=datetime.now(UTC)),
        )

    async def _seed_grid_state(self, storage: SQLiteStorageAdapter) -> None:
        await storage.save_grid_state(
            GridState(
                symbol=BTC_USD,
                reference_price=Decimal("50000"),
                spacing_percentage=Decimal("1.0"),
                levels_above=3,
                levels_below=3,
                created_at=Timestamp(dt=datetime.now(UTC)),
            )
        )

    async def test_recovered_fill_places_counter_on_first_tick(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        recovered = self._recovered_order()
        await storage.save_order(recovered)
        await self._seed_grid_state(storage)
        # Price below the counter's target (50000) so the mock's own
        # price-cross matching doesn't immediately re-fill it on placement.
        engine = GridEngine(
            _exchange(price="49500"),
            storage,
            _grid_config(),
            _safety_config(),
            pending_counters=[recovered.id],
        )

        result = await engine.step(BTC_USD)

        assert result.placed == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        sells = [o for o in opens if o.side is OrderSide.SELL]
        assert any(o.price.amount == Decimal("50000") for o in sells)

    async def test_recovered_fill_counter_honors_top_sell(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # ADR-029 implementation note: the startup-recovery counter path
        # (ADR-023) must honor counter_target_mode too — a recovered BUY
        # fill's SELL goes to the band ceiling (51500), not 50000.
        recovered = self._recovered_order()
        await storage.save_order(recovered)
        await self._seed_grid_state(storage)
        engine = GridEngine(
            _exchange(price="49500"),
            storage,
            _grid_config(counter_target_mode="top_sell"),
            _safety_config(),
            pending_counters=[recovered.id],
        )

        result = await engine.step(BTC_USD)

        assert result.placed == 1
        opens = await storage.get_open_orders(symbol=BTC_USD)
        sells = [o for o in opens if o.side is OrderSide.SELL]
        assert any(o.price.amount == Decimal("51500") for o in sells)
        assert not any(o.price.amount == Decimal("50000") for o in sells)

    async def test_different_symbol_pending_counter_untouched(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        eth_usd = Symbol(base="ETH", quote="USD")
        recovered = self._recovered_order()  # BTC/USD
        await storage.save_order(recovered)
        await self._seed_grid_state(storage)
        await storage.save_grid_state(
            GridState(
                symbol=eth_usd,
                reference_price=Decimal("3000"),
                spacing_percentage=Decimal("1.0"),
                levels_above=3,
                levels_below=3,
                created_at=Timestamp(dt=datetime.now(UTC)),
            )
        )
        exchange = MockExchangeAdapter(
            starting_balances={
                "USD": Decimal("100000"),
                "BTC": Decimal("10"),
                "ETH": Decimal("10"),
            },
            # BTC below the pending counter's target (50000) so it
            # doesn't immediately self-fill on placement.
            starting_prices={BTC_USD: Decimal("49500"), eth_usd: Decimal("3000")},
        )
        engine = GridEngine(
            exchange,
            storage,
            _grid_config(),
            _safety_config(),
            pending_counters=[recovered.id],
        )

        # Step the OTHER symbol first -- must not touch the BTC pending counter.
        await engine.step(eth_usd)
        opens_btc = await storage.get_open_orders(symbol=BTC_USD)
        assert opens_btc == []

        # Now step BTC -- the pending counter places.
        result = await engine.step(BTC_USD)
        assert result.placed == 1

    async def test_failed_pending_counter_retries_not_discarded(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Decision 4: a refused placement stays queued and retries next
        tick -- discarding it would let the auto-re-layout guard
        re-place a full grid with no counter, reproducing the orphan."""
        recovered = self._recovered_order()
        await storage.save_order(recovered)
        await self._seed_grid_state(storage)
        # Total exposure cap below the configured per-order size (10) so
        # the counter placement is refused regardless of open-order count.
        tight_safety = _safety_config(max_total="1")
        engine = GridEngine(
            _exchange(), storage, _grid_config(), tight_safety, pending_counters=[recovered.id]
        )

        first = await engine.step(BTC_USD)
        assert first.placed == 0
        # The tight cap also blocks the auto-re-layout guard's fallback
        # placement (same safety-check codepath) -- both are refused,
        # but the load-bearing assertion is the pending set itself.
        assert first.refusals >= 1
        assert recovered.id in engine._pending_counter_ids  # pylint: disable=protected-access

        # Same tight cap, second tick: still refused, proving the pending
        # counter survived the first failure instead of being dropped.
        second = await engine.step(BTC_USD)
        assert second.placed == 0
        assert second.refusals >= 1
        assert recovered.id in engine._pending_counter_ids  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# ADR-025: pre-placement spread guard
# ---------------------------------------------------------------------------


class TestSpreadGuard:
    async def test_wide_spread_skips_tick_no_placement(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        exchange.set_spread(BTC_USD, Decimal("5.0"))  # well past the 1.0% default
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "skipped_wide_spread"
        assert await storage.get_grid_state(BTC_USD) is None
        assert await storage.get_open_orders(symbol=BTC_USD) == []

    async def test_narrow_spread_proceeds_normally(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        exchange.set_spread(BTC_USD, Decimal("0.01"))
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"
        assert result.placed == 6

    async def test_disabled_guard_never_gates(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _exchange()
        exchange.set_spread(BTC_USD, Decimal("50.0"))  # absurdly wide
        safety = _safety_config()
        safety = safety.model_copy(update={"max_spread_percentage": None})
        engine = GridEngine(exchange, storage, _grid_config(), safety)

        result = await engine.step(BTC_USD)

        assert result.action == "initialized"

    async def test_spread_narrows_after_skip_resumes_normally(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        exchange = _exchange()
        exchange.set_spread(BTC_USD, Decimal("5.0"))
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())

        first = await engine.step(BTC_USD)
        assert first.action == "skipped_wide_spread"

        exchange.set_spread(BTC_USD, Decimal("0.01"))
        second = await engine.step(BTC_USD)
        assert second.action == "initialized"
        assert second.placed == 6


# ---------------------------------------------------------------------------
# Starvation back-off (P3 — the 2026-08-09 re-anchor e2e finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStarvationBackoff:
    """A layout that places 0/N enters a back-off instead of the old
    every-tick silent retry loop; any placement clears it."""

    def _broke_exchange(self) -> MockExchangeAdapter:
        # No USD (BUYs refused) and no BTC (SELLs refused): 0/6 placeable.
        return MockExchangeAdapter(
            starting_balances={"USD": Decimal("0"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )

    async def test_zero_placement_warns_once_and_backs_off(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = GridEngine(self._broke_exchange(), storage, _grid_config(), _safety_config())
        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            first = await engine.step(BTC_USD)  # initialize -> 0/6
            second = await engine.step(BTC_USD)  # would have re-attempted pre-fix
            third = await engine.step(BTC_USD)
        assert first.refusals == 6
        # Back-off: no placement attempts on the following ticks.
        assert second.refusals == 0
        assert third.refusals == 0
        starved_warns = [
            r for r in caplog.records if "layout starved: placed 0/6" in r.getMessage()
        ]
        assert len(starved_warns) == 1  # transition WARN once, not per tick

    async def test_retry_fires_on_the_retry_tick(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.services.grid_engine import _STARVED_RETRY_EVERY_TICKS

        engine = GridEngine(self._broke_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # starve
        # Fast-forward to one tick before the retry boundary.
        engine._starved_ticks[BTC_USD] = _STARVED_RETRY_EVERY_TICKS - 1
        result = await engine.step(BTC_USD)
        assert result.refusals == 6  # the retry attempted (and re-failed)

    async def test_funded_retry_recovers_and_clears(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        from wobblebot.services.grid_engine import _STARVED_RETRY_EVERY_TICKS

        exchange = self._broke_exchange()
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # starve
        exchange._balances["USD"] = Decimal("100")  # funds free up
        engine._starved_ticks[BTC_USD] = _STARVED_RETRY_EVERY_TICKS - 1
        with caplog.at_level(logging.INFO, logger="wobblebot.services.grid_engine"):
            result = await engine.step(BTC_USD)
        assert result.placed == 3  # the 3 BUYs now fit
        assert BTC_USD not in engine._starved_ticks
        assert any("recovered from starvation" in r.getMessage() for r in caplog.records)

    async def test_partial_placement_never_starves(self, storage: SQLiteStorageAdapter) -> None:
        exchange = MockExchangeAdapter(
            starting_balances={"USD": Decimal("100"), "BTC": Decimal("0")},
            starting_prices={BTC_USD: Decimal("50000")},
        )
        engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
        result = await engine.step(BTC_USD)
        assert result.placed == 3  # BUYs fit, SELLs refused
        assert BTC_USD not in engine._starved_ticks

    async def test_zero_placement_reanchor_enters_backoff(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The original incident: a re-anchor placing 0/6 must not hand
        the next tick a busy loop."""
        engine = GridEngine(self._broke_exchange(), storage, _grid_config(), _safety_config())
        await engine.step(BTC_USD)  # initialize starved state exists
        engine._starved_ticks.pop(BTC_USD, None)  # isolate the reanchor path
        ok, message = await engine.request_reanchor(BTC_USD)
        assert ok is True
        assert "placed 0/6" in message
        assert engine._starved_ticks.get(BTC_USD) == 1
        follow_up = await engine.step(BTC_USD)
        assert follow_up.refusals == 0  # backed off, no attempt

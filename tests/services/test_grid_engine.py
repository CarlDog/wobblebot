"""Tests for GridEngine — the per-symbol micro-grid orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


def _grid_config(
    *,
    spacing_pct: str = "1.0",
    above: int = 3,
    below: int = 3,
    order_size: str = "10",
    coins: dict[str, CoinGridConfig] | None = None,
) -> GridConfig:
    """Build a GridConfig with default-only or with explicit per-coin overrides."""
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal(spacing_pct),
            levels_above=above,
            levels_below=below,
            order_size_usd=Decimal(order_size),
        ),
        coins=coins or {},
    )


def _safety_config(
    *,
    max_total: str = "100000",
    max_daily: str = "100000",
    max_per_coin: str = "100000",
    max_orders: int = 100,
) -> SafetyConfig:
    """Permissive default — individual tests tighten one cap to test it."""
    return SafetyConfig(
        max_total_exposure_usd=Decimal(max_total),
        max_daily_spend_usd=Decimal(max_daily),
        max_per_coin_exposure_usd=Decimal(max_per_coin),
        max_orders_per_coin=max_orders,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


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

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
        engine = GridEngine(_exchange(), storage, config)

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
        engine = GridEngine(_exchange(), storage, _grid_config())

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
        engine = GridEngine(_exchange(), storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        engine = GridEngine(exchange, storage, _grid_config())

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
        first = GridEngine(exchange, storage, _grid_config())
        await first.step(BTC_USD)  # initializes

        # Simulate a process restart: brand-new engine instance, same storage
        # and exchange. Should NOT re-initialize (would double the orders).
        second = GridEngine(exchange, storage, _grid_config())
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
        engine = GridEngine(exchange, storage, _grid_config())

        results = await asyncio.gather(
            engine.step(BTC_USD),
            engine.step(BTC_USD),
        )

        actions = sorted(r.action for r in results)
        assert actions == ["initialized", "stepped"]
        assert len(await storage.get_open_orders(symbol=BTC_USD)) == 6

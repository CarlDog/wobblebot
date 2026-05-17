"""Stage 2.2 end-to-end integration test for the grid engine.

Composes ``GridEngine`` + ``MockExchangeAdapter`` + ``SQLiteStorageAdapter``
end-to-end, drives a synthetic ~1000-tick oscillating price walk, and
asserts the whole pipeline behaves as the design doc and ADR-006 say
it should:

  - cycles complete as expected (one BUY+SELL pair per oscillation),
  - realized P&L is positive after fees,
  - safety caps never trip (no refusals at the configured ceiling),
  - a restart of the engine against the same storage picks up where
    it left off (does NOT re-anchor or duplicate the initial layout).

Mock-only (no network), but marked ``integration`` per the established
convention — it exercises every layer together rather than one unit.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from tests.fixtures import grid_config as _shared_grid_config
from tests.fixtures import safety_config as _shared_safety_config
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import GridConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.services.grid_engine import GridEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")
REFERENCE_PRICE = Decimal("50000")
SPACING_PERCENTAGE = Decimal("1.0")  # 1% = $500 spacing
ORDER_SIZE_USD = Decimal("10")
LEVELS_ABOVE = 3
LEVELS_BELOW = 3

# Oscillation prices that cleanly cross the closest BUY (49500) and
# the closest SELL (50500) per cycle — wider than spacing so the
# closest level always fills, narrower than 2x spacing so deeper
# levels don't fill.
OSCILLATION_LOW = Decimal("49400")
OSCILLATION_HIGH = Decimal("50100")  # crosses SELL at 50000 (counter from BUY 49500)

# 500 oscillation pairs → 500 cycles → 1000 ticks of meaningful action.
NUM_OSCILLATIONS = 500


def _grid_config() -> GridConfig:
    return _shared_grid_config(
        spacing_pct=str(SPACING_PERCENTAGE),
        above=LEVELS_ABOVE,
        below=LEVELS_BELOW,
        order_size=str(ORDER_SIZE_USD),
    )


def _safety_config_loose() -> SafetyConfig:
    """Caps wide enough to never trip during the 1000-tick walk."""
    # 6 layout × $10 = $60 base; 500 BUY placements × $10 = $5000.
    return _shared_safety_config(
        max_total="1000",
        max_daily="100000",
        max_per_coin="1000",
        max_orders=20,  # layout (6) + counters in-flight; ample headroom
    )


def _exchange() -> MockExchangeAdapter:
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal("100000"), "BTC": Decimal("10")},
        starting_prices={BTC_USD: REFERENCE_PRICE},
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _run_oscillation(
    engine: GridEngine,
    exchange: MockExchangeAdapter,
    oscillations: int,
) -> tuple[int, int, int]:
    """Drive the engine through ``oscillations`` price round-trips.

    Returns ``(total_fills, total_counters_placed, total_refusals)``
    accumulated across every step. Each oscillation is two ticks
    (LOW then HIGH).
    """
    total_fills = 0
    total_counters = 0
    total_refusals = 0
    for _ in range(oscillations):
        for price in (OSCILLATION_LOW, OSCILLATION_HIGH):
            exchange.set_price(BTC_USD, price)
            result = await engine.step(BTC_USD)
            total_fills += result.fills
            total_counters += result.counters_placed
            total_refusals += result.refusals
    return total_fills, total_counters, total_refusals


async def test_oscillating_price_walk_full_pipeline(
    storage: SQLiteStorageAdapter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exchange = _exchange()
    engine = GridEngine(exchange, storage, _grid_config(), _safety_config_loose())

    # Capture engine logs to verify per-fill events are emitted.
    with caplog.at_level(logging.INFO, logger="wobblebot.services.grid_engine"):
        # Tick 0: initialize the grid at the reference.
        init_result = await engine.step(BTC_USD)
        assert init_result.action == "initialized"
        assert init_result.placed == LEVELS_ABOVE + LEVELS_BELOW
        assert init_result.refusals == 0

        fills, counters, refusals = await _run_oscillation(engine, exchange, NUM_OSCILLATIONS)

    # ---- Cycle accounting ----
    # Every oscillation completes one BUY + one SELL. Both fill and
    # both spawn a counter, so fills == counters.
    assert fills == NUM_OSCILLATIONS * 2
    assert counters == NUM_OSCILLATIONS * 2
    assert refusals == 0  # caps wide enough to never trip

    # ---- Final layout ----
    # After N complete cycles we're back to the initial 6 open orders
    # at their original levels (one per side per cycle returns to
    # identity).
    open_orders = await storage.get_open_orders(symbol=BTC_USD)
    assert len(open_orders) == LEVELS_ABOVE + LEVELS_BELOW
    open_levels = sorted((o.price.amount, o.side.value) for o in open_orders)
    assert open_levels == sorted(
        [
            (Decimal("48500"), "buy"),
            (Decimal("49000"), "buy"),
            (Decimal("49500"), "buy"),
            (Decimal("50500"), "sell"),
            (Decimal("51000"), "sell"),
            (Decimal("51500"), "sell"),
        ]
    )

    # ---- Realized P&L ----
    # Each cycle: BUY at 49500 then SELL at 50000 (counter), with mock
    # fee 0.26%. Net per cycle = (50000 - 49500) * (10/49500)
    # - 0.26% * (10 + 10.10) ≈ $0.050. Over 500 cycles, ~$25.
    # We just assert positive — the precise math is verified in unit
    # tests of the mock adapter and engine.
    trades = await storage.get_trades(symbol=BTC_USD, limit=NUM_OSCILLATIONS * 4)
    buy_trades = [t for t in trades if t.side is OrderSide.BUY]
    sell_trades = [t for t in trades if t.side is OrderSide.SELL]
    assert len(buy_trades) == NUM_OSCILLATIONS
    assert len(sell_trades) == NUM_OSCILLATIONS

    realized_pnl = sum((t.cost - t.fee for t in sell_trades), Decimal("0")) - sum(
        (t.cost + t.fee for t in buy_trades), Decimal("0")
    )
    assert realized_pnl > Decimal("0"), f"Expected positive P&L, got {realized_pnl}"

    # ---- Per-fill log events emitted ----
    fill_logs = [r for r in caplog.records if "grid fill" in r.getMessage()]
    assert len(fill_logs) == fills


async def test_restart_resume_does_not_double_initialize(
    storage: SQLiteStorageAdapter,
) -> None:
    """A fresh engine pointed at storage with an existing GridState
    must continue, not re-anchor or place a duplicate layout."""
    exchange = _exchange()

    # Phase 1: original engine runs init + one oscillation.
    engine_a = GridEngine(exchange, storage, _grid_config(), _safety_config_loose())
    init = await engine_a.step(BTC_USD)
    assert init.action == "initialized"
    await _run_oscillation(engine_a, exchange, oscillations=1)

    state_after_a = await storage.get_grid_state(BTC_USD)
    open_after_a = await storage.get_open_orders(symbol=BTC_USD)
    trades_after_a = await storage.get_trades(symbol=BTC_USD)
    assert state_after_a is not None

    # Phase 2: brand-new engine instance, same storage + exchange.
    engine_b = GridEngine(exchange, storage, _grid_config(), _safety_config_loose())
    second_step = await engine_b.step(BTC_USD)

    # First step on the fresh engine must NOT re-initialize.
    assert second_step.action == "stepped"

    # GridState row is unchanged.
    state_after_b = await storage.get_grid_state(BTC_USD)
    assert state_after_b is not None
    assert state_after_b.reference_price == state_after_a.reference_price
    assert state_after_b.created_at.dt == state_after_a.created_at.dt

    # No new open orders introduced by the no-op step.
    open_after_b = await storage.get_open_orders(symbol=BTC_USD)
    assert len(open_after_b) == len(open_after_a)

    # Trade history did not gain spurious entries.
    trades_after_b = await storage.get_trades(symbol=BTC_USD)
    assert len(trades_after_b) == len(trades_after_a)

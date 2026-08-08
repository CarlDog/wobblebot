"""Tests for tools/auditor.py (ADR-028, P2 slice 4).

Pins the three judge corrections plus the end-to-end replay shape:

1. Daily-cap neutering: a config whose max_daily_spend_usd would stall
   a replay (wall-clock window!) must not suppress later BUYs.
2. AuditorExchangeAdapter defers fill-at-placement: an order placed
   while the current price already crosses it does NOT fill in the
   same call; the next set_price fills it.
3. Anchor warm-start at bar-0 open: the first BUY level prices off the
   first bar's OPEN, not its close (asserted via the fill's limit
   price through the injected-storage seam).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from tools.auditor import AuditorExchangeAdapter, _replay_symbol
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import (
    Amount,
    OHLCBar,
    OrderSide,
    Price,
    Symbol,
    Timestamp,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _buy_order(price: str) -> Order:
    return Order(
        symbol=Symbol(base="BTC", quote="USD"),
        side=OrderSide.BUY,
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal("0.01"), asset="BTC"),
        created_at=Timestamp(dt=datetime(2026, 6, 1, tzinfo=UTC)),
    )


_BTC = Symbol(base="BTC", quote="USD")
_T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


def _bar(index: int, o: str, h: str, l: str, c: str) -> OHLCBar:  # noqa: E741
    return OHLCBar(
        symbol=_BTC,
        interval_minutes=1,
        opened_at=_T0 + timedelta(minutes=index),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        vwap=Decimal("0"),
        volume=Decimal("1"),
        count=1,
    )


def _grid_config(spacing: str = "1.0") -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal(spacing),
            levels_above=1,
            levels_below=1,
            order_size_usd=Decimal("10"),
        ),
        coins={},
    )


def _safety_config(daily: str = "1000000000") -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("1000"),
        max_daily_spend_usd=Decimal(daily),
        max_per_coin_exposure_usd=Decimal("1000"),
        max_orders_per_coin=20,
    )


class TestAuditorExchangeAdapter:
    async def test_placement_does_not_fill_immediately(self) -> None:
        """Correction 2: current price 99 already crosses a BUY@100 —
        the stock mock fills it inside place_order; the auditor
        subclass must NOT."""
        adapter = AuditorExchangeAdapter(
            starting_balances={"USD": Decimal("1000")},
            starting_prices={_BTC: Decimal("99")},
        )
        order = _buy_order("100")
        placed = await adapter.place_order(order)
        assert placed.status == "open"
        assert len(await adapter.get_trade_history(_BTC)) == 0
        # The NEXT price step fills it.
        fills = adapter.set_price(_BTC, Decimal("99"))
        assert len(fills) == 1

    async def test_set_price_matching_still_works(self) -> None:
        """The suppression is placement-scoped only — normal set_price
        matching is inherited unchanged."""
        adapter = AuditorExchangeAdapter(
            starting_balances={"USD": Decimal("1000")},
            starting_prices={_BTC: Decimal("200")},
        )
        await adapter.place_order(_buy_order("100"))
        assert adapter.set_price(_BTC, Decimal("150")) == []
        assert len(adapter.set_price(_BTC, Decimal("100"))) == 1


@pytest_asyncio.fixture
async def replay_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


# A bar sequence engineered for one full cycle at 1% spacing off a 100
# anchor: BUY level 99, SELL counter ~99.99 (99 * 1.01).
# bar 0: flat at the anchor -- grid lays out (BUY@99; SELL@101 refused,
#        seed_base=0).
# bar 1: dips to 98.5 -- fills BUY@99; counter SELL@99.99 placed.
# bar 2: rises to 100.5 -- fills the counter SELL -> one complete cycle.
_CYCLE_BARS = [
    _bar(0, "100", "100.2", "99.8", "100"),
    _bar(1, "100", "100.1", "99.0", "99.5"),
    _bar(2, "99.5", "100.5", "99.4", "100.4"),
]


class TestReplaySymbol:
    async def test_full_cycle_end_to_end(self) -> None:
        result = await _replay_symbol(
            _CYCLE_BARS,
            _BTC,
            grid_config=_grid_config(),
            safety_config=_safety_config(),
            seed_usd=Decimal("1000"),
            seed_base=Decimal("0"),
            fee_rate=Decimal("0.0026"),
        )
        assert result.buys == 1
        assert result.sells == 1
        assert result.cycle_count == 1
        assert result.fees > 0
        assert result.bars_replayed == 3
        # ~1% cycle on a $10 order minus two fees: small positive PnL.
        assert result.realized_pnl > 0

    async def test_daily_cap_is_neutered_upstream(self) -> None:
        """Correction 1 pin: _replay_symbol itself honors whatever
        safety config it is given — a tight daily cap DOES suppress
        placements (this is the wall-clock poisoning ADR-028 warns
        about). The neutering therefore lives in _run; this documents
        why it must. Two BUY levels at $10 each: a $15 daily cap
        refuses the second at layout; the neutered config places both.
        seed_base=1 keeps the SELL leg placeable so balance refusals
        can't pollute the count."""
        two_below = GridConfig(
            default=GridLevels(
                spacing_percentage=Decimal("1.0"),
                levels_above=1,
                levels_below=2,
                order_size_usd=Decimal("10"),
            ),
            coins={},
        )
        flat = [_bar(0, "100", "100.2", "99.8", "100")]
        tight = await _replay_symbol(
            flat,
            _BTC,
            grid_config=two_below,
            safety_config=_safety_config(daily="15"),
            seed_usd=Decimal("1000"),
            seed_base=Decimal("1"),
            fee_rate=Decimal("0.0026"),
        )
        neutered = await _replay_symbol(
            flat,
            _BTC,
            grid_config=two_below,
            safety_config=_safety_config(),
            seed_usd=Decimal("1000"),
            seed_base=Decimal("1"),
            fee_rate=Decimal("0.0026"),
        )
        assert tight.refusals > 0
        assert neutered.refusals == 0

    async def test_anchor_warm_starts_at_bar0_open(
        self, replay_storage: SQLiteStorageAdapter
    ) -> None:
        """Correction 3: bar 0 opens at 100 but closes at 105. Anchored
        at open, the BUY level is 99 (100 * 0.99); anchored at close it
        would be 103.95. The bar-1 dip fills the BUY — its limit price
        proves which anchor was used."""
        bars = [
            _bar(0, "100", "105.5", "99.9", "105"),
            _bar(1, "105", "105.2", "98.0", "99.0"),
        ]
        await _replay_symbol(
            bars,
            _BTC,
            grid_config=_grid_config(),
            safety_config=_safety_config(),
            seed_usd=Decimal("1000"),
            seed_base=Decimal("0"),
            fee_rate=Decimal("0.0026"),
            replay_storage=replay_storage,
        )
        trades = await replay_storage.get_trades(symbol=_BTC, limit=10)
        buy_prices = {t.price.amount for t in trades if t.side is OrderSide.BUY}
        assert Decimal("99.00") in buy_prices or Decimal("99") in buy_prices
        assert Decimal("103.95") not in buy_prices

    async def test_portfolio_math_flat_market_loses_nothing(self) -> None:
        """A dead-flat replay places orders but fills nothing: net PnL
        exactly 0, no fees, no drawdown."""
        flat = [_bar(i, "100", "100.4", "99.6", "100") for i in range(5)]
        result = await _replay_symbol(
            flat,
            _BTC,
            grid_config=_grid_config(),
            safety_config=_safety_config(),
            seed_usd=Decimal("500"),
            seed_base=Decimal("0"),
            fee_rate=Decimal("0.0026"),
        )
        assert result.buys == 0
        assert result.fees == 0
        assert result.net_pnl_usd == 0
        assert result.max_drawdown == 0

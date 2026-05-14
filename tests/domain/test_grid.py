"""Tests for the pure grid math in ``wobblebot.domain.grid``."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from wobblebot.domain.grid import (
    GridLevel,
    GridSlot,
    compute_grid_levels,
    grid_spacing,
    is_offside,
    next_counter_action,
)
from wobblebot.domain.value_objects import OrderSide, Symbol

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# GridLevel and GridSlot value-object behavior
# ---------------------------------------------------------------------------


class TestGridLevel:
    def test_construction(self) -> None:
        lvl = GridLevel(side=OrderSide.BUY, price=Decimal("100"))
        assert lvl.side == OrderSide.BUY
        assert lvl.price == Decimal("100")

    def test_frozen(self) -> None:
        lvl = GridLevel(side=OrderSide.BUY, price=Decimal("100"))
        with pytest.raises(ValidationError):
            lvl.price = Decimal("200")  # type: ignore[misc]


class TestGridSlot:
    def _slot(self, order_id: object = None) -> GridSlot:
        return GridSlot(
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide.BUY,
            level_price=Decimal("99"),
            order_id=order_id,  # type: ignore[arg-type]
        )

    def test_empty_slot(self) -> None:
        slot = self._slot(order_id=None)
        assert slot.is_empty is True
        assert slot.order_id is None

    def test_occupied_slot(self) -> None:
        oid = uuid4()
        slot = self._slot(order_id=oid)
        assert slot.is_empty is False
        assert slot.order_id == oid

    def test_default_order_id_is_none(self) -> None:
        slot = GridSlot(
            symbol=Symbol(base="BTC", quote="USD"),
            side=OrderSide.SELL,
            level_price=Decimal("101"),
        )
        assert slot.is_empty is True

    def test_frozen(self) -> None:
        slot = self._slot()
        with pytest.raises(ValidationError):
            slot.order_id = uuid4()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Layout computation
# ---------------------------------------------------------------------------


class TestGridSpacing:
    def test_one_percent_of_one_hundred_is_one(self) -> None:
        assert grid_spacing(Decimal("100"), Decimal("1")) == Decimal("1")

    def test_half_percent_of_fifty_thousand(self) -> None:
        # Realistic BTC price + tight spacing
        assert grid_spacing(Decimal("50000"), Decimal("0.5")) == Decimal("250")

    def test_decimal_precision_preserved(self) -> None:
        # 1.5% of 100.123 must not lose precision via float coercion
        result = grid_spacing(Decimal("100.123"), Decimal("1.5"))
        assert result == Decimal("100.123") * Decimal("1.5") / Decimal("100")


class TestComputeGridLevels:
    def test_symmetric_grid_around_one_hundred(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=3,
            levels_below=3,
        )
        # Sorted ascending, BUYs below ref, SELLs above, ref itself excluded
        assert [lvl.price for lvl in levels] == [
            Decimal("97"),
            Decimal("98"),
            Decimal("99"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
        ]
        assert [lvl.side for lvl in levels] == [
            OrderSide.BUY,
            OrderSide.BUY,
            OrderSide.BUY,
            OrderSide.SELL,
            OrderSide.SELL,
            OrderSide.SELL,
        ]

    def test_asymmetric_grid(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("50000"),
            spacing_percentage=Decimal("0.5"),
            levels_above=2,
            levels_below=4,
        )
        prices = [lvl.price for lvl in levels]
        sides = [lvl.side for lvl in levels]
        assert len(levels) == 6
        assert sides == [OrderSide.BUY] * 4 + [OrderSide.SELL] * 2
        # Below: 49000, 49250, 49500, 49750. Above: 50250, 50500.
        assert prices == [
            Decimal("49000.0"),
            Decimal("49250.0"),
            Decimal("49500.0"),
            Decimal("49750.0"),
            Decimal("50250.0"),
            Decimal("50500.0"),
        ]

    def test_zero_below_only_sells(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=2,
            levels_below=0,
        )
        assert all(lvl.side == OrderSide.SELL for lvl in levels)
        assert [lvl.price for lvl in levels] == [Decimal("101"), Decimal("102")]

    def test_zero_above_only_buys(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=0,
            levels_below=2,
        )
        assert all(lvl.side == OrderSide.BUY for lvl in levels)
        assert [lvl.price for lvl in levels] == [Decimal("98"), Decimal("99")]

    def test_zero_both_sides_returns_empty(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=0,
            levels_below=0,
        )
        assert levels == []

    def test_reference_price_not_in_grid(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=5,
            levels_below=5,
        )
        assert all(lvl.price != Decimal("100") for lvl in levels)

    def test_levels_are_strictly_sorted(self) -> None:
        levels = compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=10,
            levels_below=10,
        )
        prices = [lvl.price for lvl in levels]
        assert prices == sorted(prices)
        # And strictly increasing — no duplicates
        assert len(set(prices)) == len(prices)


# ---------------------------------------------------------------------------
# Reactive math: counter-orders and offside detection
# ---------------------------------------------------------------------------


class TestNextCounterAction:
    def test_buy_fill_yields_sell_one_spacing_up(self) -> None:
        result = next_counter_action(
            filled_side=OrderSide.BUY,
            filled_price=Decimal("99"),
            spacing=Decimal("1"),
        )
        assert result == GridLevel(side=OrderSide.SELL, price=Decimal("100"))

    def test_sell_fill_yields_buy_one_spacing_down(self) -> None:
        result = next_counter_action(
            filled_side=OrderSide.SELL,
            filled_price=Decimal("101"),
            spacing=Decimal("1"),
        )
        assert result == GridLevel(side=OrderSide.BUY, price=Decimal("100"))

    def test_pure_arithmetic_does_not_clamp_to_grid(self) -> None:
        # Counter at the very top of a grid window has no idea it's at
        # the top; that's the engine's job.
        result = next_counter_action(
            filled_side=OrderSide.BUY,
            filled_price=Decimal("9999999"),
            spacing=Decimal("1"),
        )
        assert result.price == Decimal("10000000")

    def test_counter_round_trip_returns_to_start(self) -> None:
        # BUY 99 → counter SELL 100 → that SELL filling produces a BUY 99 again.
        first = next_counter_action(OrderSide.BUY, Decimal("99"), Decimal("1"))
        second = next_counter_action(first.side, first.price, Decimal("1"))
        assert second == GridLevel(side=OrderSide.BUY, price=Decimal("99"))


class TestIsOffside:
    def _grid(self) -> list[GridLevel]:
        return compute_grid_levels(
            reference_price=Decimal("100"),
            spacing_percentage=Decimal("1"),
            levels_above=3,
            levels_below=3,
        )

    def test_inside_window_not_offside(self) -> None:
        assert is_offside(Decimal("100"), self._grid()) is False
        assert is_offside(Decimal("97"), self._grid()) is False  # at lowest BUY
        assert is_offside(Decimal("103"), self._grid()) is False  # at highest SELL

    def test_below_lowest_buy_is_offside(self) -> None:
        assert is_offside(Decimal("96.99"), self._grid()) is True
        assert is_offside(Decimal("0.01"), self._grid()) is True

    def test_above_highest_sell_is_offside(self) -> None:
        assert is_offside(Decimal("103.01"), self._grid()) is True
        assert is_offside(Decimal("999999"), self._grid()) is True

    def test_empty_grid_always_offside(self) -> None:
        assert is_offside(Decimal("100"), []) is True

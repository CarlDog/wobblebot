"""Tests for the pure cost-basis math in ``wobblebot.domain.cost_basis`` (ADR-032)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.domain.cost_basis import CostBasis, assess_sell, replay_average_cost
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp

pytestmark = pytest.mark.unit

_SYMBOL = Symbol(base="BTC", quote="USD")
_MAKER_FEE_RATE = Decimal("0.0026")


def _trade(
    *,
    side: OrderSide,
    price: str,
    amount: str,
    fee: str,
    minutes_offset: int = 0,
) -> Trade:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    qty = Decimal(amount)
    px = Decimal(price)
    return Trade(
        id=f"T-{side.value}-{minutes_offset}",
        order_id=f"O-{side.value}-{minutes_offset}",
        symbol=_SYMBOL,
        side=side,
        price=Price(amount=px, currency=_SYMBOL.quote),
        amount=Amount(value=qty, asset=_SYMBOL.base),
        fee=Decimal(fee),
        cost=px * qty,
        executed_at=Timestamp(dt=base_time + timedelta(minutes=minutes_offset)),
    )


class TestReplayAverageCost:
    def test_no_trades_yields_unknown_basis(self) -> None:
        basis = replay_average_cost([])
        assert basis.quantity == Decimal("0")
        assert basis.average_cost is None

    def test_single_buy_capitalizes_fee_into_basis(self) -> None:
        # 1 BTC @ 100 + fee 0.26 -> basis = 100.26 / 1 = 100.26
        buy = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0.26")
        basis = replay_average_cost([buy])
        assert basis.quantity == Decimal("1")
        assert basis.average_cost == Decimal("100.26")

    def test_two_buys_average_correctly(self) -> None:
        # 1 BTC @ 100 (+0 fee) then 1 BTC @ 200 (+0 fee) -> avg 150 over 2 BTC
        buy1 = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0", minutes_offset=0)
        buy2 = _trade(side=OrderSide.BUY, price="200", amount="1", fee="0", minutes_offset=1)
        basis = replay_average_cost([buy1, buy2])
        assert basis.quantity == Decimal("2")
        assert basis.average_cost == Decimal("150")

    def test_sell_reduces_quantity_without_changing_average(self) -> None:
        buy = _trade(side=OrderSide.BUY, price="100", amount="2", fee="0", minutes_offset=0)
        sell = _trade(side=OrderSide.SELL, price="150", amount="1", fee="0", minutes_offset=1)
        basis = replay_average_cost([buy, sell])
        assert basis.quantity == Decimal("1")
        assert basis.average_cost == Decimal("100")

    def test_sell_beyond_tracked_quantity_clamps_to_zero(self) -> None:
        """A SELL for more than tracked history (pre-existing inventory)
        must not go negative or corrupt a later BUY's basis."""
        buy = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0", minutes_offset=0)
        oversized_sell = _trade(
            side=OrderSide.SELL, price="150", amount="5", fee="0", minutes_offset=1
        )
        next_buy = _trade(side=OrderSide.BUY, price="80", amount="1", fee="0", minutes_offset=2)
        basis = replay_average_cost([buy, oversized_sell, next_buy])
        assert basis.quantity == Decimal("1")
        assert basis.average_cost == Decimal("80")

    def test_trades_out_of_order_are_replayed_chronologically(self) -> None:
        buy = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0", minutes_offset=5)
        sell = _trade(side=OrderSide.SELL, price="150", amount="1", fee="0", minutes_offset=10)
        # Passed in reverse order -- replay must sort by executed_at first.
        basis = replay_average_cost([sell, buy])
        assert basis.quantity == Decimal("0")
        assert basis.average_cost is None

    def test_full_liquidation_resets_basis(self) -> None:
        buy = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0", minutes_offset=0)
        sell = _trade(side=OrderSide.SELL, price="150", amount="1", fee="0", minutes_offset=1)
        basis = replay_average_cost([buy, sell])
        assert basis.quantity == Decimal("0")
        assert basis.average_cost is None


class TestAssessSell:
    def test_unknown_basis_always_allowed(self) -> None:
        result = assess_sell(
            Decimal("50"),
            CostBasis(quantity=Decimal("0"), average_cost=None),
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is True
        assert result.reason == "unknown_basis"

    def test_sell_at_basis_price_incurs_one_side_fee_loss(self) -> None:
        """A SELL at exactly a bare average cost (no fee baked in) loses
        just the proposed sell's own maker fee (~0.26%) -- clears the
        default 1.0% tolerance."""
        basis = CostBasis(quantity=Decimal("1"), average_cost=Decimal("100"))
        result = assess_sell(
            Decimal("100"),
            basis,
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is True
        assert result.loss_percentage is not None
        assert Decimal("0.25") < result.loss_percentage < Decimal("0.27")

    def test_sell_at_replayed_buy_price_incurs_round_trip_fee_loss(self) -> None:
        """ADR-032: "a sell at exactly the buy price scores ~= 0.52% loss"
        -- the round-trip figure only appears when the basis itself came
        from replay_average_cost, which already capitalized the BUY's own
        fee. Composes replay + assess to pin that documented number."""
        buy = _trade(side=OrderSide.BUY, price="100", amount="1", fee="0.26")
        basis = replay_average_cost([buy])
        result = assess_sell(
            Decimal("100"),
            basis,
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is True
        assert result.loss_percentage is not None
        assert Decimal("0.51") < result.loss_percentage < Decimal("0.53")

    def test_sell_below_tolerance_is_deferred(self) -> None:
        basis = CostBasis(quantity=Decimal("1"), average_cost=Decimal("100"))
        result = assess_sell(
            Decimal("95"),  # 5% below basis, well past a 1.0% tolerance
            basis,
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is False
        assert result.reason == "below_cost_basis"
        assert result.average_cost == Decimal("100")

    def test_sell_above_basis_always_allowed(self) -> None:
        basis = CostBasis(quantity=Decimal("1"), average_cost=Decimal("100"))
        result = assess_sell(
            Decimal("120"),
            basis,
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is True

    def test_boundary_at_exactly_max_loss_percentage_is_allowed(self) -> None:
        # Strictly-greater-than comparison: exactly at the tolerance clears.
        basis = CostBasis(quantity=Decimal("1"), average_cost=Decimal("100"))
        net_proceeds_for_exact_tolerance = Decimal("100") * (
            Decimal("1") - Decimal("1.0") / Decimal("100")
        )
        proposed_price = net_proceeds_for_exact_tolerance / (Decimal("1") - _MAKER_FEE_RATE)
        result = assess_sell(
            proposed_price,
            basis,
            max_loss_percentage=Decimal("1.0"),
            maker_fee_rate=_MAKER_FEE_RATE,
        )
        assert result.allowed is True

"""Unit tests for services.metrics — pure math, deterministic golden cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import stdev

import pytest

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.metrics import (
    CycleStats,
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)

pytestmark = pytest.mark.unit


BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


def _d(value: str) -> Decimal:
    return Decimal(value)


def _make_trade(
    *,
    side: OrderSide,
    cost: str,
    fee: str = "0",
    symbol: Symbol | None = None,
    minute_offset: int = 0,
    trade_id: str | None = None,
) -> Trade:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    return Trade(
        id=trade_id or f"T-{side.value}-{minute_offset}-{cost}",
        order_id=f"O-{side.value}-{minute_offset}",
        symbol=symbol or BTC_USD,
        side=side,
        price=Price(amount=_d("80000"), currency="USD"),
        amount=Amount(value=_d("0.0001"), asset="BTC"),
        fee=_d(fee),
        cost=_d(cost),
        executed_at=Timestamp(dt=base + timedelta(minutes=minute_offset)),
    )


class TestVolatility:
    def test_empty_returns_zero(self) -> None:
        assert compute_volatility([]) == _d("0")

    def test_single_returns_zero(self) -> None:
        assert compute_volatility([_d("100")]) == _d("0")

    def test_two_prices_returns_zero_because_one_return_is_undefined_stdev(self) -> None:
        # One simple-return value has no defined sample stdev (n-1 = 0).
        # Documented behaviour: return zero, let caller distinguish by len().
        assert compute_volatility([_d("100"), _d("101")]) == _d("0")

    def test_constant_prices_zero_vol(self) -> None:
        assert compute_volatility([_d("100")] * 10) == _d("0")

    def test_known_returns_match_statistics_stdev(self) -> None:
        prices = [_d("100"), _d("105"), _d("103"), _d("110"), _d("108")]
        # Expected: stdev of [0.05, -2/105, 7/103, -2/110]
        returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
        expected = Decimal(stdev(returns))
        assert compute_volatility(prices) == expected

    def test_scale_invariance(self) -> None:
        # 1%, -1%, 1%, -1% sequence should give identical vol at any price level
        a = [_d("100"), _d("101"), _d("99.99"), _d("100.9899"), _d("99.979002")]
        b = [_d("10000"), _d("10100"), _d("9999"), _d("10098.99"), _d("9997.9002")]
        assert compute_volatility(a) == compute_volatility(b)

    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            compute_volatility([_d("100"), _d("-1")])

    def test_zero_price_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            compute_volatility([_d("100"), _d("0")])


class TestMaxDrawdown:
    def test_empty_returns_zero(self) -> None:
        assert compute_max_drawdown([]) == _d("0")

    def test_single_returns_zero(self) -> None:
        assert compute_max_drawdown([_d("100")]) == _d("0")

    def test_monotonically_rising_returns_zero(self) -> None:
        assert compute_max_drawdown([_d("100"), _d("101"), _d("102"), _d("103")]) == _d("0")

    def test_simple_peak_to_trough(self) -> None:
        # Peak 200, trough 100 → -50%
        assert compute_max_drawdown([_d("100"), _d("200"), _d("100")]) == _d("-0.5")

    def test_takes_worst_drawdown_not_last(self) -> None:
        # Peak 200 dropping to 100 (-50%), then peak 110 dropping to 105 (~-4.5%).
        # Worst is the first drawdown.
        result = compute_max_drawdown([_d("100"), _d("200"), _d("100"), _d("110"), _d("105")])
        assert result == _d("-0.5")

    def test_running_peak_resets_correctly(self) -> None:
        # Series climbs to a new peak before drawing down — drawdown measured
        # from the new peak, not the original.
        result = compute_max_drawdown([_d("100"), _d("150"), _d("200"), _d("160")])
        assert result == _d("-0.2")  # (160 - 200) / 200

    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            compute_max_drawdown([_d("100"), _d("-50")])


class TestFlatness:
    def test_empty_returns_one(self) -> None:
        # Per docstring contract: trivial flatness for <2 prices.
        assert compute_flatness([]) == _d("1")

    def test_single_returns_one(self) -> None:
        assert compute_flatness([_d("100")]) == _d("1")

    def test_constant_prices_perfectly_flat(self) -> None:
        assert compute_flatness([_d("100")] * 5) == _d("1")

    def test_known_range_over_mean(self) -> None:
        # mean = 100, range = 10 → flatness = 1 - 10/100 = 0.9
        prices = [_d("95"), _d("100"), _d("105"), _d("100"), _d("100")]
        assert compute_flatness(prices) == _d("0.9")

    def test_clamps_at_zero_for_wide_range(self) -> None:
        # mean = 100, range = 180 → raw = 1 - 1.8 = -0.8 → clamps to 0
        prices = [_d("10"), _d("100"), _d("190")]
        assert compute_flatness(prices) == _d("0")

    def test_order_independent(self) -> None:
        # Same values shuffled — flatness uses max/min/mean only.
        a = [_d("95"), _d("100"), _d("105"), _d("100")]
        b = [_d("100"), _d("105"), _d("95"), _d("100")]
        assert compute_flatness(a) == compute_flatness(b)

    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            compute_flatness([_d("100"), _d("-1")])


class TestCycleStats:
    def test_empty_trades_returns_all_zeros(self) -> None:
        result = compute_cycle_stats([])
        assert result == CycleStats(
            cycle_count=0,
            win_count=0,
            win_rate=_d("0"),
            total_pnl=_d("0"),
            avg_profit_per_cycle=_d("0"),
        )

    def test_only_buys_no_cycles(self) -> None:
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10.00", minute_offset=0),
            _make_trade(side=OrderSide.BUY, cost="10.00", minute_offset=1),
        ]
        assert compute_cycle_stats(trades).cycle_count == 0

    def test_only_sells_no_cycles(self) -> None:
        # Selling pre-existing position — no completed cycles by definition.
        trades = [_make_trade(side=OrderSide.SELL, cost="11.00", minute_offset=0)]
        assert compute_cycle_stats(trades).cycle_count == 0

    def test_single_profitable_cycle(self) -> None:
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10.00", fee="0.04", minute_offset=0),
            _make_trade(side=OrderSide.SELL, cost="11.00", fee="0.04", minute_offset=1),
        ]
        result = compute_cycle_stats(trades)
        # PnL = 11.00 - 10.00 - 0.04 - 0.04 = 0.92
        assert result.cycle_count == 1
        assert result.win_count == 1
        assert result.win_rate == _d("1")
        assert result.total_pnl == _d("0.92")
        assert result.avg_profit_per_cycle == _d("0.92")

    def test_single_losing_cycle(self) -> None:
        # Sell below buy → losing cycle
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10.00", fee="0.04", minute_offset=0),
            _make_trade(side=OrderSide.SELL, cost="9.50", fee="0.04", minute_offset=1),
        ]
        result = compute_cycle_stats(trades)
        assert result.cycle_count == 1
        assert result.win_count == 0
        assert result.win_rate == _d("0")
        assert result.total_pnl == _d("-0.58")
        assert result.avg_profit_per_cycle == _d("-0.58")

    def test_break_even_cycle_counts_as_loss(self) -> None:
        # PnL exactly 0 from fees alone doesn't count as "win" (win_rate uses >0).
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10.00", fee="0", minute_offset=0),
            _make_trade(side=OrderSide.SELL, cost="10.00", fee="0", minute_offset=1),
        ]
        result = compute_cycle_stats(trades)
        assert result.cycle_count == 1
        assert result.win_count == 0
        assert result.total_pnl == _d("0")

    def test_fifo_matching_oldest_buy_first(self) -> None:
        # Two buys then two sells — sells pop oldest-buy first.
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10", fee="0", minute_offset=0, trade_id="B1"),
            _make_trade(side=OrderSide.BUY, cost="11", fee="0", minute_offset=1, trade_id="B2"),
            _make_trade(side=OrderSide.SELL, cost="12", fee="0", minute_offset=2, trade_id="S1"),
            _make_trade(side=OrderSide.SELL, cost="13", fee="0", minute_offset=3, trade_id="S2"),
        ]
        result = compute_cycle_stats(trades)
        # Cycle 1: B1 (10) → S1 (12) → +2
        # Cycle 2: B2 (11) → S2 (13) → +2
        assert result.cycle_count == 2
        assert result.total_pnl == _d("4")
        assert result.win_rate == _d("1")

    def test_unmatched_buys_excluded(self) -> None:
        # Two buys, one sell — second buy remains open (no cycle).
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10", minute_offset=0),
            _make_trade(side=OrderSide.BUY, cost="11", minute_offset=1),
            _make_trade(side=OrderSide.SELL, cost="12", minute_offset=2),
        ]
        result = compute_cycle_stats(trades)
        assert result.cycle_count == 1

    def test_unmatched_sells_skipped(self) -> None:
        # Sell first, then buy — sell has no matching prior buy → skipped.
        trades = [
            _make_trade(side=OrderSide.SELL, cost="12", minute_offset=0),
            _make_trade(side=OrderSide.BUY, cost="10", minute_offset=1),
        ]
        assert compute_cycle_stats(trades).cycle_count == 0

    def test_per_symbol_isolation(self) -> None:
        # BTC buy then ETH sell — different symbols, no cycle.
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10", symbol=BTC_USD, minute_offset=0),
            _make_trade(side=OrderSide.SELL, cost="11", symbol=ETH_USD, minute_offset=1),
        ]
        assert compute_cycle_stats(trades).cycle_count == 0

    def test_per_symbol_independent_cycles(self) -> None:
        # Two cycles: one BTC, one ETH.
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10", symbol=BTC_USD, minute_offset=0),
            _make_trade(side=OrderSide.BUY, cost="100", symbol=ETH_USD, minute_offset=1),
            _make_trade(side=OrderSide.SELL, cost="11", symbol=BTC_USD, minute_offset=2),
            _make_trade(side=OrderSide.SELL, cost="105", symbol=ETH_USD, minute_offset=3),
        ]
        result = compute_cycle_stats(trades)
        assert result.cycle_count == 2
        assert result.total_pnl == _d("6")

    def test_mixed_win_loss(self) -> None:
        # Three cycles: +2, -1, +3 → total +4, win_rate 2/3
        trades = [
            _make_trade(side=OrderSide.BUY, cost="10", minute_offset=0),
            _make_trade(side=OrderSide.SELL, cost="12", minute_offset=1),  # +2
            _make_trade(side=OrderSide.BUY, cost="13", minute_offset=2),
            _make_trade(side=OrderSide.SELL, cost="12", minute_offset=3),  # -1
            _make_trade(side=OrderSide.BUY, cost="11", minute_offset=4),
            _make_trade(side=OrderSide.SELL, cost="14", minute_offset=5),  # +3
        ]
        result = compute_cycle_stats(trades)
        assert result.cycle_count == 3
        assert result.win_count == 2
        assert result.win_rate == _d("2") / _d("3")
        assert result.total_pnl == _d("4")
        assert result.avg_profit_per_cycle == _d("4") / _d("3")

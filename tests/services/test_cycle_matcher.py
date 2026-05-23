"""Tests for cycle matching: FIFO pairing + today-PnL aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.services.cycle_matcher import (
    RecentCycle,
    match_cycles,
    today_realized_pnl,
)

pytestmark = [pytest.mark.unit]


_BTC_USD = Symbol(base="BTC", quote="USD")
_ETH_USD = Symbol(base="ETH", quote="USD")


def _trade(
    *,
    side: str,
    price: str,
    amount: str = "0.000129",
    fee: str = "0.04",
    when: datetime,
    symbol: Symbol = _BTC_USD,
    trade_id: str | None = None,
) -> Trade:
    return Trade(
        id=trade_id or f"T-{when.isoformat()}-{side}",
        order_id=f"O-{when.isoformat()}",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal(price), currency=symbol.quote),
        amount=Amount(value=Decimal(amount), asset=symbol.base),
        fee=Decimal(fee),
        cost=Decimal(price) * Decimal(amount),
        executed_at=Timestamp(dt=when),
    )


class TestMatchCyclesHappyPath:
    def test_single_complete_cycle(self) -> None:
        """One BUY then one SELL = one cycle."""
        buy_at = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        sell_at = buy_at + timedelta(minutes=30)
        cycles = match_cycles(
            [
                _trade(side="buy", price="77000", when=buy_at),
                _trade(side="sell", price="77800", when=sell_at),
            ]
        )
        assert len(cycles) == 1
        c = cycles[0]
        assert c.symbol == _BTC_USD
        assert c.buy_price.amount == Decimal("77000")
        assert c.sell_price.amount == Decimal("77800")
        # net = (77800 - 77000) * 0.000129 - 0.04 - 0.04
        #     = 800 * 0.000129 - 0.08 = 0.1032 - 0.08 = 0.0232
        assert c.net_pnl == pytest.approx(Decimal("0.0232"), abs=Decimal("0.0001"))

    def test_fifo_pairing_across_multiple_cycles(self) -> None:
        """3 BUYs then 3 SELLs pair oldest-with-oldest."""
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="buy", price="77000", when=base + timedelta(minutes=0)),
            _trade(side="buy", price="76900", when=base + timedelta(minutes=10)),
            _trade(side="buy", price="76800", when=base + timedelta(minutes=20)),
            _trade(side="sell", price="77800", when=base + timedelta(minutes=30)),
            _trade(side="sell", price="77700", when=base + timedelta(minutes=40)),
            _trade(side="sell", price="77600", when=base + timedelta(minutes=50)),
        ]
        cycles = match_cycles(trades)
        assert len(cycles) == 3
        # Returned newest-first by sell time.
        sell_times = [c.sell_executed_at.dt for c in cycles]
        assert sell_times == sorted(sell_times, reverse=True)
        # FIFO: oldest sell pairs with oldest cheaper buy.
        oldest_cycle = cycles[-1]
        assert oldest_cycle.buy_price.amount == Decimal("77000")
        assert oldest_cycle.sell_price.amount == Decimal("77800")

    def test_per_symbol_isolation(self) -> None:
        """BTC and ETH cycles don't cross-match."""
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="buy", price="77000", when=base, symbol=_BTC_USD),
            _trade(side="buy", price="3000", when=base, symbol=_ETH_USD),
            _trade(side="sell", price="3100", when=base + timedelta(minutes=10), symbol=_ETH_USD),
            _trade(side="sell", price="77800", when=base + timedelta(minutes=20), symbol=_BTC_USD),
        ]
        cycles = match_cycles(trades)
        assert len(cycles) == 2
        symbols = {c.symbol for c in cycles}
        assert symbols == {_BTC_USD, _ETH_USD}

    def test_input_ordering_robust(self) -> None:
        """Trades in arbitrary order produce the same cycles."""
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="sell", price="77800", when=base + timedelta(minutes=30)),
            _trade(side="buy", price="77000", when=base),
        ]
        cycles = match_cycles(trades)
        assert len(cycles) == 1


class TestMatchCyclesEdgeCases:
    def test_orphan_sell_dropped(self) -> None:
        """A SELL with no cheaper BUY in the window is silently dropped."""
        sell_at = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        cycles = match_cycles([_trade(side="sell", price="77800", when=sell_at)])
        assert cycles == []

    def test_inflight_buy_not_in_cycles(self) -> None:
        """A BUY without a matching SELL doesn't appear as a cycle."""
        buy_at = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        cycles = match_cycles([_trade(side="buy", price="77000", when=buy_at)])
        assert cycles == []

    def test_sell_below_any_buy_is_orphan(self) -> None:
        """If a SELL is cheaper than ALL pending BUYs, it stays orphan
        (no synthesized cycle at a loss). Operationally rare — the
        grid sells above its buys — but worth verifying."""
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="buy", price="80000", when=base),
            _trade(side="sell", price="77800", when=base + timedelta(minutes=10)),
        ]
        cycles = match_cycles(trades)
        assert cycles == []

    def test_empty_input(self) -> None:
        assert match_cycles([]) == []

    def test_only_buys(self) -> None:
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="buy", price="77000", when=base),
            _trade(side="buy", price="76900", when=base + timedelta(minutes=10)),
        ]
        assert match_cycles(trades) == []

    def test_only_sells(self) -> None:
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            _trade(side="sell", price="77800", when=base),
        ]
        assert match_cycles(trades) == []


class TestTodayRealizedPnl:
    def test_sums_cycles_whose_sell_fired_today(self) -> None:
        now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
        cycles = [
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=now - timedelta(hours=4)),
                sell_executed_at=Timestamp(dt=now - timedelta(hours=1)),
                buy_price=Price(amount=Decimal("77000"), currency="USD"),
                sell_price=Price(amount=Decimal("77800"), currency="USD"),
                amount=Amount(value=Decimal("0.000129"), asset="BTC"),
                buy_fee=Decimal("0.04"),
                sell_fee=Decimal("0.04"),
                net_pnl=Decimal("0.10"),
            ),
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=now - timedelta(hours=6)),
                sell_executed_at=Timestamp(dt=now - timedelta(hours=2)),
                buy_price=Price(amount=Decimal("76800"), currency="USD"),
                sell_price=Price(amount=Decimal("77600"), currency="USD"),
                amount=Amount(value=Decimal("0.000129"), asset="BTC"),
                buy_fee=Decimal("0.04"),
                sell_fee=Decimal("0.04"),
                net_pnl=Decimal("0.13"),
            ),
        ]
        assert today_realized_pnl(cycles, now=now) == Decimal("0.23")

    def test_excludes_cycles_whose_sell_fired_yesterday(self) -> None:
        now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
        yesterday = now - timedelta(days=1)
        cycles = [
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=yesterday - timedelta(hours=2)),
                sell_executed_at=Timestamp(dt=yesterday),
                buy_price=Price(amount=Decimal("77000"), currency="USD"),
                sell_price=Price(amount=Decimal("77800"), currency="USD"),
                amount=Amount(value=Decimal("0.000129"), asset="BTC"),
                buy_fee=Decimal("0.04"),
                sell_fee=Decimal("0.04"),
                net_pnl=Decimal("0.10"),
            ),
        ]
        assert today_realized_pnl(cycles, now=now) == Decimal("0")

    def test_buy_yesterday_sell_today_counts(self) -> None:
        """Cycle's PnL is realized at the SELL — counts toward the
        SELL's day, even if the BUY was yesterday."""
        now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
        cycles = [
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=now - timedelta(days=1, hours=5)),
                sell_executed_at=Timestamp(dt=now - timedelta(hours=1)),
                buy_price=Price(amount=Decimal("77000"), currency="USD"),
                sell_price=Price(amount=Decimal("77800"), currency="USD"),
                amount=Amount(value=Decimal("0.000129"), asset="BTC"),
                buy_fee=Decimal("0.04"),
                sell_fee=Decimal("0.04"),
                net_pnl=Decimal("0.10"),
            ),
        ]
        assert today_realized_pnl(cycles, now=now) == Decimal("0.10")

    def test_empty_cycles_returns_zero(self) -> None:
        now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
        assert today_realized_pnl([], now=now) == Decimal("0")

    def test_uses_now_when_not_provided(self) -> None:
        """Defaults to datetime.now(UTC) — verifies the helper is
        callable without an explicit time-of-day override."""
        cycles: list[RecentCycle] = []
        result = today_realized_pnl(cycles)
        assert result == Decimal("0")

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

    def test_amount_match_beats_price_fifo(self) -> None:
        """Engine pairs counter-orders by amount, not price-FIFO.

        Regression for 2026-05-23 reconciliation: a SELL straddled by
        two cheaper BUYs must pair with the BUY whose amount matches
        (the engine's actual counter), not the older cheaper BUY by
        FIFO. Without amount-match the dashboard fabricates a fake
        loss cycle out of a real winning one.
        """
        base = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
        trades = [
            # BUY #2 — cheaper than the SELL but only by $15; the
            # engine's counter for this would be ~$77,628, not $76,874.
            _trade(
                side="buy",
                price="76859.50",
                amount="0.00013010",
                when=base + timedelta(minutes=0),
            ),
            # BUY #3 — much cheaper AND its amount matches the SELL.
            # This is the engine's actual pair.
            _trade(
                side="buy",
                price="76105.80",
                amount="0.00013139",
                when=base + timedelta(minutes=30),
            ),
            # SELL with BUY #3's amount.
            _trade(
                side="sell",
                price="76874.60",
                amount="0.00013139",
                when=base + timedelta(hours=2),
            ),
        ]
        cycles = match_cycles(trades)
        assert len(cycles) == 1
        c = cycles[0]
        # Must pair with BUY #3 (price 76105.80), NOT BUY #2 (76859.50).
        assert c.buy_price.amount == Decimal("76105.80"), (
            f"Expected pair with BUY #3 @ 76105.80, got BUY @ {c.buy_price.amount} "
            "(FIFO-cheapest regression — matcher must amount-match first)"
        )
        # And the cycle must be a win, not a fake loss.
        assert c.net_pnl > 0, f"Engine cycle should be profitable; got {c.net_pnl}"


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

    def test_operator_tz_filters_by_local_day_not_utc(self) -> None:
        """Regression for 2026-05-23 evening bug.

        After UTC midnight but before local midnight, a cycle that
        fired earlier "today" in the operator's tz fell outside the
        UTC-day filter, silently showing "Today: $0.00" on the
        dashboard. Filter must scope by the operator's tz when
        provided. With ``tz_name="America/Chicago"`` (UTC-5/-6 CST/
        CDT), a cycle whose SELL fired at 20:55 UTC on May 23
        (= 15:55 CDT on May 23) is "today" if we're now at 04:03 UTC
        May 24 (= 23:03 CDT May 23).
        """
        # Now: 04:03 UTC May 24 — past UTC midnight, but only ~11pm
        # local on May 23 in Chicago.
        now = datetime(2026, 5, 24, 4, 3, tzinfo=UTC)
        # Cycle: SELL at 20:55 UTC May 23 (= 15:55 CDT May 23).
        sell_at_utc = datetime(2026, 5, 23, 20, 55, tzinfo=UTC)
        cycles = [
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=sell_at_utc - timedelta(hours=2)),
                sell_executed_at=Timestamp(dt=sell_at_utc),
                buy_price=Price(amount=Decimal("76105.80"), currency="USD"),
                sell_price=Price(amount=Decimal("76874.60"), currency="USD"),
                amount=Amount(value=Decimal("0.00013139"), asset="BTC"),
                buy_fee=Decimal("0.025"),
                sell_fee=Decimal("0.02525"),
                net_pnl=Decimal("0.0508"),
            ),
        ]
        # UTC behavior: cycle is "yesterday" → 0
        assert today_realized_pnl(cycles, now=now) == Decimal("0")
        # America/Chicago behavior: cycle IS today → 0.0508
        assert today_realized_pnl(
            cycles, now=now, tz_name="America/Chicago"
        ) == Decimal("0.0508")

    def test_unknown_tz_falls_back_to_utc(self) -> None:
        """An invalid IANA name must NOT raise — settings page
        validates on write, but the renderer must stay robust."""
        now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
        cycles = [
            RecentCycle(
                symbol=_BTC_USD,
                buy_executed_at=Timestamp(dt=now - timedelta(hours=2)),
                sell_executed_at=Timestamp(dt=now - timedelta(hours=1)),
                buy_price=Price(amount=Decimal("77000"), currency="USD"),
                sell_price=Price(amount=Decimal("77800"), currency="USD"),
                amount=Amount(value=Decimal("0.000129"), asset="BTC"),
                buy_fee=Decimal("0.04"),
                sell_fee=Decimal("0.04"),
                net_pnl=Decimal("0.10"),
            ),
        ]
        # Both fire today UTC, so a bogus tz that falls back to UTC
        # still sees them as today.
        assert today_realized_pnl(
            cycles, now=now, tz_name="Not/A_Real_Zone"
        ) == Decimal("0.10")

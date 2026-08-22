"""Capital Reporter — ADR-040 stage 1.

ADR-040's validation plan makes one demand of this suite: the Reporter
must independently reproduce all three findings the 2026-08-22 session
proved from production data. A check that cannot rediscover a defect we
already know is real is not trusted. Those three live in
``TestReproducesKnownFindings`` with the real numbers hard-coded; the
rest of the file covers the boundaries around them.

Real values used below, all from that day:

- SOL: order_size_usd $5, price band ~$86-103, Kraken ordermin 0.06.
  Held 0.05876515 SOL — 98% of the minimum, so it can neither enter
  nor exit.
- XRP/account: $113.88 bought, $108.32 sold, $5.57 net out, cap charged
  ~$118 of $120.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import (
    Amount,
    OrderSide,
    PairLimits,
    Price,
    Symbol,
    Timestamp,
)
from wobblebot.services.capital_reporter import (
    CapitalReport,
    check_entry_viability,
    check_exit_viability,
    compute_cap_honesty,
    summarize,
    utc_day_start,
)

_SOL = Symbol(base="SOL", quote="USD")
_XRP = Symbol(base="XRP", quote="USD")

# Kraken's real minimums, 2026-08-22.
_SOL_LIMITS = PairLimits(symbol=_SOL, ordermin=Decimal("0.06"), costmin=Decimal("0.5"))
_XRP_LIMITS = PairLimits(symbol=_XRP, ordermin=Decimal("10"), costmin=Decimal("0.5"))

_NOW = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)


def _trade(symbol: Symbol, side: str, cost: str, *, when: datetime | None = None) -> Trade:
    moment = when or (_NOW - timedelta(hours=1))
    return Trade(
        id=f"T-{side}-{cost}-{moment.timestamp()}",
        order_id="O-1",
        symbol=symbol,
        side=OrderSide(side),
        price=Price(amount=Decimal("1"), currency="USD"),
        amount=Amount(value=Decimal(cost), asset=symbol.base),
        fee=Decimal("0"),
        cost=Decimal(cost),
        executed_at=Timestamp(dt=moment),
    )


class TestReproducesKnownFindings:
    """ADR-040's gate. These are not synthetic cases."""

    def test_sol_cannot_enter_at_any_level(self) -> None:
        """$5/order against SOL's real 2026-08-22 grid: 0/6 placeable.

        The engine logged exactly this — 'placed 0/6 (6 refused)' with
        every refusal citing ordermin 0.06.
        """
        result = check_entry_viability(
            _SOL,
            order_size_usd=Decimal("5.0"),
            # Anchor + geometry that produced the observed 86.02-103.04 ladder.
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        assert result.total_levels == 6
        assert result.fully_blocked, "SOL must be reported as blocked at EVERY level"
        assert result.required_order_size_usd is not None
        # Needs ~$6.19 to clear 0.06 at the top level (~$103.04).
        assert Decimal("6.1") < result.required_order_size_usd < Decimal("6.3")

    def test_sol_cannot_exit_either(self) -> None:
        """The check nobody would have specified from first principles.

        0.05876515 held against a 0.06 minimum — 98% of the way there,
        and completely unsellable as a single order.
        """
        result = check_exit_viability(
            _SOL,
            held_quantity=Decimal("0.05876515"),
            limits=_SOL_LIMITS,
            source="exchange",
        )
        assert result.blocked
        assert result.source == "exchange"

    def test_daily_cap_charges_21x_the_capital_that_moved(self) -> None:
        """The account's real 2026-08-22 flow.

        $113.88 out, $108.32 back — 95% recycled, $5.57 net — against a
        cap reading ~$118 consumed.
        """
        trades = [
            _trade(_XRP, "buy", "113.88"),
            _trade(_XRP, "sell", "108.32"),
        ]
        cap = compute_cap_honesty(
            trades,
            charged_usd=Decimal("118.02"),
            cap_usd=Decimal("120.00"),
            now=_NOW,
        )
        assert cap.net_deployed_usd == Decimal("5.56")
        assert cap.recycled_usd == Decimal("108.32")
        ratio = cap.overstatement_ratio
        assert ratio is not None
        assert Decimal("20") < ratio < Decimal("22"), f"expected ~21x, got {ratio}"
        assert cap.consumed_fraction is not None
        assert cap.consumed_fraction > Decimal("0.98")


class TestEntryViability:
    def test_adequate_size_is_not_a_finding(self) -> None:
        result = check_entry_viability(
            _SOL,
            order_size_usd=Decimal("10.0"),
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        assert not result.blocked
        assert result.required_order_size_usd is None

    def test_the_required_size_actually_clears_every_level(self) -> None:
        """The remedy the report prints must be sufficient, not merely
        larger. Feed it back in and the finding must disappear."""
        blocked = check_entry_viability(
            _SOL,
            order_size_usd=Decimal("5.0"),
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        assert blocked.required_order_size_usd is not None
        retried = check_entry_viability(
            _SOL,
            order_size_usd=blocked.required_order_size_usd,
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        assert not retried.blocked

    def test_partial_blocking_is_reported_as_partial(self) -> None:
        """Only the top levels fail when size sits between the band's
        ends — the report must not overstate it as a total outage."""
        result = check_entry_viability(
            _SOL,
            order_size_usd=Decimal("5.6"),
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        assert result.blocked
        assert not result.fully_blocked
        assert 0 < len(result.blocked_prices) < result.total_levels

    def test_costmin_blocks_independently_of_ordermin(self) -> None:
        """An order can clear the base-currency floor and still fail the
        quote-currency one."""
        limits = PairLimits(symbol=_XRP, ordermin=Decimal("0"), costmin=Decimal("25"))
        result = check_entry_viability(
            _XRP,
            order_size_usd=Decimal("5.0"),
            reference_price=Decimal("1.45"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=limits,
        )
        assert result.fully_blocked
        assert result.required_order_size_usd == Decimal("25")


class TestExitViability:
    def test_zero_position_is_not_a_finding(self) -> None:
        """Nothing held means nothing stranded — reporting it would be
        noise on every symbol the bot has fully sold out of."""
        result = check_exit_viability(
            _SOL, held_quantity=Decimal("0"), limits=_SOL_LIMITS, source="exchange"
        )
        assert not result.blocked

    def test_position_at_the_minimum_is_sellable(self) -> None:
        result = check_exit_viability(
            _SOL, held_quantity=Decimal("0.06"), limits=_SOL_LIMITS, source="exchange"
        )
        assert not result.blocked

    def test_replay_source_is_carried_through(self) -> None:
        """The 2026-08-22 incident proved replay can disagree with the
        exchange, so a finding must say which number it used."""
        result = check_exit_viability(
            _SOL, held_quantity=Decimal("0.01"), limits=_SOL_LIMITS, source="replay"
        )
        assert result.blocked
        assert result.source == "replay"


class TestCapHonesty:
    def test_only_todays_trades_count(self) -> None:
        """The cap resets at UTC midnight; so must the measurement."""
        trades = [
            _trade(_XRP, "buy", "50", when=_NOW - timedelta(days=1)),
            _trade(_XRP, "buy", "10"),
        ]
        cap = compute_cap_honesty(
            trades, charged_usd=Decimal("10"), cap_usd=Decimal("120"), now=_NOW
        )
        assert cap.gross_bought_usd == Decimal("10")

    def test_day_boundary_is_utc_midnight(self) -> None:
        assert utc_day_start(_NOW) == datetime(2026, 8, 22, 0, 0, tzinfo=UTC)

    def test_net_selling_day_has_no_ratio(self) -> None:
        """A net-selling day is a DIFFERENT finding, not a bigger one.

        Reporting a huge ratio here would read as a worse defect; the
        summary line handles it separately.
        """
        trades = [_trade(_XRP, "buy", "10"), _trade(_XRP, "sell", "40")]
        cap = compute_cap_honesty(
            trades, charged_usd=Decimal("10"), cap_usd=Decimal("120"), now=_NOW
        )
        assert cap.net_deployed_usd < 0
        assert cap.overstatement_ratio is None

    def test_zero_cap_yields_no_fraction(self) -> None:
        cap = compute_cap_honesty([], charged_usd=Decimal("10"), cap_usd=Decimal("0"), now=_NOW)
        assert cap.consumed_fraction is None


class TestSummarize:
    def test_clean_report_says_nothing(self) -> None:
        """Silence on a healthy account. A reporter that always emits
        something trains the operator to ignore it."""
        report = CapitalReport(entry=(), exits=(), cap=None)
        assert list(summarize(report)) == []
        assert not report.has_findings

    def test_every_finding_names_its_symbol_and_remedy(self) -> None:
        entry = check_entry_viability(
            _SOL,
            order_size_usd=Decimal("5.0"),
            reference_price=Decimal("94.53"),
            spacing_percentage=Decimal("3.0"),
            levels_above=3,
            levels_below=3,
            limits=_SOL_LIMITS,
        )
        exit_ = check_exit_viability(
            _SOL, held_quantity=Decimal("0.05876515"), limits=_SOL_LIMITS, source="exchange"
        )
        report = CapitalReport(entry=(entry,), exits=(exit_,), cap=None)
        lines = list(summarize(report))
        assert len(lines) == 2
        assert all("SOL/USD" in line for line in lines)
        assert "needs >=" in lines[0]
        assert "cannot be sold as a single order" in lines[1]

    def test_net_selling_day_gets_its_own_line(self) -> None:
        trades = [_trade(_XRP, "buy", "10"), _trade(_XRP, "sell", "40")]
        cap = compute_cap_honesty(
            trades, charged_usd=Decimal("118"), cap_usd=Decimal("120"), now=_NOW
        )
        lines = list(summarize(CapitalReport(entry=(), exits=(), cap=cap)))
        assert len(lines) == 1
        assert "net-flat-or-selling day" in lines[0]

    def test_small_overstatement_is_not_reported(self) -> None:
        """A cap charging slightly more than net is normal grid
        behaviour, not a finding. Only a material gap earns a line."""
        trades = [_trade(_XRP, "buy", "100"), _trade(_XRP, "sell", "20")]
        cap = compute_cap_honesty(
            trades, charged_usd=Decimal("100"), cap_usd=Decimal("120"), now=_NOW
        )
        ratio = cap.overstatement_ratio
        assert ratio is not None and ratio < 2
        assert list(summarize(CapitalReport(entry=(), exits=(), cap=cap))) == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_trade_cost_not_order_notional(side: str) -> None:
    """Flow is measured from executed cost: an order that never filled
    moved no capital, and counting it would recreate the very
    overstatement this check exists to detect."""
    cap = compute_cap_honesty(
        [_trade(_XRP, side, "42")],
        charged_usd=Decimal("0"),
        cap_usd=Decimal("120"),
        now=_NOW,
    )
    total = cap.gross_bought_usd + cap.gross_sold_usd
    assert total == Decimal("42")

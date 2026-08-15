"""Pin the sweep-order rules (services/symbol_priority).

The bug being fixed is quiet and cumulative: cli/live swept config order,
so the first-listed symbol claimed the caps every tick. Measured in
production 2026-08-15, the gradient was monotonic down the list —
ETH 5/6, SOL 2/6, ADA 0/6 — and BTC, listed first, had first claim for
months.

An ordering bug fails the same way: nothing raises, a symbol simply never
gets funded. So the rules are pinned explicitly rather than inferred.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import Symbol
from wobblebot.services.screener import ScreenerRanking, SymbolMetrics
from wobblebot.services.symbol_priority import order_symbols, proximity_in_atr

pytestmark = pytest.mark.unit

BTC = Symbol(base="BTC", quote="USD")
ETH = Symbol(base="ETH", quote="USD")
SOL = Symbol(base="SOL", quote="USD")
ADA = Symbol(base="ADA", quote="USD")


def _ranking(symbol: Symbol, composite: float) -> ScreenerRanking:
    return ScreenerRanking(
        metrics=SymbolMetrics(
            symbol=symbol, volatility=0.004, flatness=0.9, atr_pct=0.7, bar_count=400
        ),
        vol_rank=1,
        flatness_rank=1,
        atr_rank=1,
        composite=composite,
    )


class TestProximity:
    def test_nearest_level_in_atr_units(self) -> None:
        # price 100, levels at 97 and 103 -> nearest is 3 away; ATR 1.5 -> 2.0
        assert proximity_in_atr(Decimal("100"), [Decimal("97"), Decimal("103")], 1.5) == 2.0

    def test_atr_normalisation_makes_scales_comparable(self) -> None:
        """The whole point: BTC 300 from a level and DOGE 0.003 from one
        are the SAME distance in the only unit that predicts a fill."""
        btc = proximity_in_atr(Decimal("63000"), [Decimal("63300")], 300.0)
        doge = proximity_in_atr(Decimal("0.070"), [Decimal("0.073")], 0.003)
        assert btc == pytest.approx(doge)

    @pytest.mark.parametrize(
        "price,levels,atr",
        [
            (None, [Decimal("1")], 1.0),  # no price
            (Decimal("1"), [], 1.0),  # no grid yet
            (Decimal("1"), [Decimal("1")], None),  # no ATR
            (Decimal("1"), [Decimal("1")], 0.0),  # degenerate ATR
        ],
    )
    def test_unknowable_sorts_last_not_first(self, price, levels, atr) -> None:  # type: ignore[no-untyped-def]
        """inf, never 0. Missing data must never be mistaken for
        imminence — that would fund the symbol we know least about."""
        assert proximity_in_atr(price, levels, atr) == math.inf


class TestOrdering:
    def test_composite_is_primary(self) -> None:
        order = order_symbols(
            [BTC, ETH, SOL],
            [_ranking(BTC, 3.0), _ranking(ETH, 1.0), _ranking(SOL, 2.0)],
            {},
        )
        assert order == [ETH, SOL, BTC]

    def test_proximity_breaks_a_composite_tie(self) -> None:
        """Composite is a mean of three integer ranks, so its values are
        discrete (k/3) and exact ties are common in a small cohort. The
        tiebreak really fires — it is not decorative."""
        order = order_symbols(
            [BTC, ETH, SOL],
            [_ranking(BTC, 2.0), _ranking(ETH, 2.0), _ranking(SOL, 2.0)],
            {BTC: 5.0, ETH: 0.2, SOL: 1.5},
        )
        assert order == [ETH, SOL, BTC]

    def test_proximity_never_overrides_a_better_composite(self) -> None:
        """Character first, timing second. A symbol sitting right on a
        level but poorly suited must not jump a well-suited one."""
        order = order_symbols(
            [BTC, ETH],
            [_ranking(BTC, 1.0), _ranking(ETH, 3.0)],
            {BTC: 9.0, ETH: 0.01},
        )
        assert order == [BTC, ETH]

    def test_unranked_symbols_sort_last(self) -> None:
        """Too few bars / no ATR = unknown. Unknown must not outrank
        measured suitability."""
        order = order_symbols([BTC, ETH, SOL], [_ranking(SOL, 9.0)], {})
        assert order[0] == SOL
        assert set(order[1:]) == {BTC, ETH}

    def test_membership_is_never_changed(self) -> None:
        """This decides ORDER, not who trades. Dropping a symbol here
        would silently stop trading it."""
        configured = [BTC, ETH, SOL, ADA]
        order = order_symbols(configured, [_ranking(ETH, 1.0)], {ETH: 0.1})
        assert sorted(str(s) for s in order) == sorted(str(s) for s in configured)
        assert len(order) == len(configured)

    def test_ordering_is_deterministic(self) -> None:
        """Same cohort -> same order, every tick. A wobbling order would
        make 'why did SOL get funded and ADA not' unanswerable."""
        rankings = [_ranking(BTC, 2.0), _ranking(ETH, 2.0)]
        first = order_symbols([BTC, ETH], rankings, {})
        for _ in range(5):
            assert order_symbols([ETH, BTC], rankings, {}) == first

    def test_empty_cohort_is_not_an_error(self) -> None:
        assert order_symbols([], [], {}) == []

    def test_no_rankings_at_all_preserves_a_stable_order(self) -> None:
        """Screener unavailable (thin bars everywhere) must degrade to a
        deterministic sweep, never to an exception."""
        order = order_symbols([SOL, BTC, ETH], [], {})
        assert len(order) == 3
        assert order == sorted(order, key=str)

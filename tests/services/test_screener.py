"""Tests for services/screener.py (P2 slice 5).

Pins the blueprint's load-bearing decisions: band-distance ranking is
NON-monotonic for volatility/ATR%, flatness ranks descending, the
composite is a mean of ranks, and correlation is an annotation that
returns None (n/a) on thin overlap rather than a fabricated number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.services.screener import (
    MIN_BARS,
    MIN_CORRELATION_OVERLAP,
    SymbolMetrics,
    bar_returns,
    compute_symbol_metrics,
    max_abs_correlation,
    pearson,
    rank_candidates,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


def _bars(symbol: Symbol, closes: list[float]) -> list[OHLCBar]:
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.5
        low = min(prev, close) - 0.5
        out.append(
            OHLCBar(
                symbol=symbol,
                interval_minutes=60,
                opened_at=_T0 + timedelta(hours=i),
                open=Decimal(str(prev)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
        )
        prev = close
    return out


def _metrics(name: str, vol: float, flat: float, atr: float) -> SymbolMetrics:
    return SymbolMetrics(
        symbol=Symbol(base=name, quote="USD"),
        volatility=vol,
        flatness=flat,
        atr_pct=atr,
        bar_count=100,
    )


class TestComputeSymbolMetrics:
    def test_too_thin_returns_none(self) -> None:
        btc = Symbol(base="BTC", quote="USD")
        assert compute_symbol_metrics(_bars(btc, [100.0] * (MIN_BARS - 1))) is None

    def test_metrics_computed(self) -> None:
        btc = Symbol(base="BTC", quote="USD")
        closes = [100.0 + (i % 5) for i in range(60)]
        metrics = compute_symbol_metrics(_bars(btc, closes))
        assert metrics is not None
        assert metrics.symbol == btc
        assert metrics.volatility > 0
        assert 0 <= metrics.flatness <= 1
        assert metrics.atr_pct > 0
        assert metrics.bar_count == 60


class TestRankCandidates:
    def test_band_distance_is_not_monotonic(self) -> None:
        """The load-bearing decision: with center 0.5%, a symbol at
        0.5% beats BOTH the too-quiet 0.1% and the too-hot 2.0% — a
        monotonic ranker could never produce this."""
        cohort = [
            _metrics("QUIET", vol=0.001, flat=0.9, atr=0.1),
            _metrics("SWEET", vol=0.005, flat=0.9, atr=0.5),
            _metrics("HOT", vol=0.020, flat=0.9, atr=2.0),
        ]
        rankings = rank_candidates(cohort, vol_band_center=0.005, atr_band_center_pct=0.5)
        assert rankings[0].metrics.symbol.base == "SWEET"
        assert rankings[0].vol_rank == 1
        assert rankings[0].atr_rank == 1

    def test_flatness_ranks_descending(self) -> None:
        cohort = [
            _metrics("RANGY", vol=0.005, flat=0.95, atr=0.5),
            _metrics("TRENDY", vol=0.005, flat=0.20, atr=0.5),
        ]
        rankings = rank_candidates(cohort, vol_band_center=0.005, atr_band_center_pct=0.5)
        by_name = {r.metrics.symbol.base: r for r in rankings}
        assert by_name["RANGY"].flatness_rank < by_name["TRENDY"].flatness_rank
        assert rankings[0].metrics.symbol.base == "RANGY"

    def test_composite_is_mean_of_ranks(self) -> None:
        cohort = [
            _metrics("A", vol=0.005, flat=0.9, atr=0.5),
            _metrics("B", vol=0.010, flat=0.5, atr=1.0),
        ]
        rankings = rank_candidates(cohort, vol_band_center=0.005, atr_band_center_pct=0.5)
        for r in rankings:
            assert r.composite == pytest.approx((r.vol_rank + r.flatness_rank + r.atr_rank) / 3.0)

    def test_empty_cohort(self) -> None:
        assert rank_candidates([], vol_band_center=0.005, atr_band_center_pct=0.5) == []

    def test_deterministic_tie_break(self) -> None:
        twins = [
            _metrics("AAA", vol=0.005, flat=0.9, atr=0.5),
            _metrics("BBB", vol=0.005, flat=0.9, atr=0.5),
        ]
        rankings = rank_candidates(twins, vol_band_center=0.005, atr_band_center_pct=0.5)
        assert rankings[0].metrics.symbol.base == "AAA"


class TestPearson:
    def test_perfect_positive(self) -> None:
        assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)

    def test_perfect_negative(self) -> None:
        assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)

    def test_known_value(self) -> None:
        # Hand-checked: cov=10, var_x=10, var_y=14.8 -> r = 10/sqrt(148).
        assert pearson([1, 2, 3, 4, 5], [2, 1, 4, 3, 6]) == pytest.approx(0.8221, abs=1e-3)

    def test_zero_variance_is_none(self) -> None:
        assert pearson([1, 1, 1], [2, 3, 4]) is None

    def test_length_mismatch_is_none(self) -> None:
        assert pearson([1, 2], [1, 2, 3]) is None


class TestCorrelationAnnotation:
    def test_aligned_identical_series_correlates_fully(self) -> None:
        btc = Symbol(base="BTC", quote="USD")
        eth = Symbol(base="ETH", quote="USD")
        closes = [100.0 + ((i * 7) % 11) for i in range(MIN_CORRELATION_OVERLAP + 2)]
        candidate = bar_returns(_bars(btc, closes))
        held = {eth: bar_returns(_bars(eth, closes))}
        result = max_abs_correlation(candidate, held)
        assert result is not None
        r, against = result
        assert r == pytest.approx(1.0)
        assert against == eth

    def test_thin_overlap_is_none(self) -> None:
        """The honest n/a: novel candidates without observed history
        must annotate as None, never a fabricated number."""
        btc = Symbol(base="BTC", quote="USD")
        eth = Symbol(base="ETH", quote="USD")
        short = [100.0, 101.0, 102.0] * 5
        candidate = bar_returns(_bars(btc, short))
        held = {eth: bar_returns(_bars(eth, short))}
        assert max_abs_correlation(candidate, held) is None

    def test_disjoint_windows_are_none(self) -> None:
        btc = Symbol(base="BTC", quote="USD")
        eth = Symbol(base="ETH", quote="USD")
        closes = [100.0 + (i % 7) for i in range(MIN_CORRELATION_OVERLAP + 2)]
        candidate = bar_returns(_bars(btc, closes))
        shifted = _bars(eth, closes)
        shifted = [
            b.model_copy(update={"opened_at": b.opened_at + timedelta(days=365)}) for b in shifted
        ]
        held = {eth: bar_returns(shifted)}
        assert max_abs_correlation(candidate, held) is None

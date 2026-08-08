"""Tests for services/ta_metrics.py (P2 slice 3).

Exact-value pins are hand-computed in comments; structural pins use
degenerate inputs whose indicator values are mathematically forced
(constant series, strict monotone trends, close-at-extreme windows).
Every public function must return None on too-short input — the
SummaryBuilder relies on that to emit null TA fields rather than
crashing on thin history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.services.ta_metrics import (
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_sma,
    compute_stochastic,
)

pytestmark = pytest.mark.unit

_BTC = Symbol(base="BTC", quote="USD")
_T0 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)


def _bars_from_closes(closes: list[float]) -> list[OHLCBar]:
    """Bars whose high/low bracket open+close; open = previous close."""
    bars = []
    prev_close = closes[0]
    for i, close in enumerate(closes):
        high = max(prev_close, close) + 1.0
        low = min(prev_close, close) - 1.0
        bars.append(
            OHLCBar(
                symbol=_BTC,
                interval_minutes=60,
                opened_at=_T0 + timedelta(hours=i),
                open=Decimal(str(prev_close)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
        )
        prev_close = close
    return bars


def _flat_bars(count: int, *, low: float = 100.0, high: float = 110.0) -> list[OHLCBar]:
    """Constant-range bars: TR is exactly ``high - low`` every bar."""
    close = (low + high) / 2
    return [
        OHLCBar(
            symbol=_BTC,
            interval_minutes=60,
            opened_at=_T0 + timedelta(hours=i),
            open=Decimal(str(close)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            vwap=Decimal("0"),
            volume=Decimal("1"),
            count=1,
        )
        for i in range(count)
    ]


class TestSMA:
    def test_exact_value(self) -> None:
        # (3 + 4 + 5) / 3 = 4.0
        assert compute_sma(_bars_from_closes([1, 2, 3, 4, 5]), 3) == 4.0

    def test_too_short_returns_none(self) -> None:
        assert compute_sma(_bars_from_closes([1, 2]), 3) is None

    def test_zero_period_returns_none(self) -> None:
        assert compute_sma(_bars_from_closes([1, 2, 3]), 0) is None


class TestEMA:
    def test_exact_value(self) -> None:
        # Seed SMA(3) = 2.0, k = 0.5: 4*.5 + 2*.5 = 3.0; 5*.5 + 3*.5 = 4.0
        assert compute_ema(_bars_from_closes([1, 2, 3, 4, 5]), 3) == 4.0

    def test_constant_series_is_constant(self) -> None:
        assert compute_ema(_bars_from_closes([7.0] * 30), 12) == 7.0

    def test_too_short_returns_none(self) -> None:
        assert compute_ema(_bars_from_closes([1, 2]), 3) is None


class TestRSI:
    def test_exact_value_alternating(self) -> None:
        # period=2, closes [1,2,1,2]: gains [1,0,1], losses [0,1,0].
        # Seed: avg_gain=.5, avg_loss=.5 -> RSI 50. Next: avg_gain=.75,
        # avg_loss=.25 -> RS=3 -> RSI 75.
        assert compute_rsi(_bars_from_closes([1, 2, 1, 2]), period=2) == 75.0

    def test_pure_uptrend_is_100(self) -> None:
        closes = [float(i) for i in range(1, 20)]
        assert compute_rsi(_bars_from_closes(closes)) == 100.0

    def test_pure_downtrend_is_0(self) -> None:
        closes = [float(i) for i in range(20, 1, -1)]
        assert compute_rsi(_bars_from_closes(closes)) == 0.0

    def test_flat_series_is_neutral_50(self) -> None:
        assert compute_rsi(_bars_from_closes([5.0] * 20)) == 50.0

    def test_too_short_returns_none(self) -> None:
        assert compute_rsi(_bars_from_closes([1.0] * 14)) is None  # needs period+1


class TestMACD:
    def test_constant_series_is_all_zero(self) -> None:
        result = compute_macd(_bars_from_closes([50.0] * 40))
        assert result is not None
        assert (result.line, result.signal, result.histogram) == (0.0, 0.0, 0.0)

    def test_histogram_is_line_minus_signal(self) -> None:
        closes = [100 + i + (5 if i % 7 == 0 else 0) for i in range(60)]
        result = compute_macd(_bars_from_closes([float(c) for c in closes]))
        assert result is not None
        assert result.histogram == pytest.approx(result.line - result.signal)

    def test_uptrend_line_positive(self) -> None:
        result = compute_macd(_bars_from_closes([float(i) for i in range(1, 60)]))
        assert result is not None
        assert result.line > 0

    def test_too_short_returns_none(self) -> None:
        # Needs slow + signal - 1 = 34 bars.
        assert compute_macd(_bars_from_closes([1.0] * 33)) is None

    def test_fast_not_below_slow_returns_none(self) -> None:
        assert compute_macd(_bars_from_closes([1.0] * 40), fast=26, slow=26) is None


class TestBollinger:
    def test_exact_values(self) -> None:
        # Window [2,3,4,5]: middle 3.5, population var 1.25, sigma
        # 1.118034; upper = 3.5 + 2*sigma = 5.736068.
        result = compute_bollinger(_bars_from_closes([1, 2, 3, 4, 5]), period=4)
        assert result is not None
        assert result.middle == 3.5
        assert result.upper == pytest.approx(5.736068, abs=1e-6)
        assert result.lower == pytest.approx(1.263932, abs=1e-6)

    def test_constant_series_collapses_bands(self) -> None:
        result = compute_bollinger(_bars_from_closes([9.0] * 25))
        assert result is not None
        assert result.upper == result.middle == result.lower == 9.0

    def test_middle_equals_sma(self) -> None:
        bars = _bars_from_closes([float(100 + (i * i) % 13) for i in range(30)])
        result = compute_bollinger(bars)
        assert result is not None
        assert result.middle == pytest.approx(compute_sma(bars, 20))

    def test_too_short_returns_none(self) -> None:
        assert compute_bollinger(_bars_from_closes([1.0] * 19)) is None


class TestATR:
    def test_constant_range_bars(self) -> None:
        # Every bar spans exactly 10 with no inter-bar gap: TR = 10.
        assert compute_atr(_flat_bars(10), period=3) == 10.0

    def test_too_short_returns_none(self) -> None:
        # period TRs need period+1 bars.
        assert compute_atr(_flat_bars(14), period=14) is None
        assert compute_atr(_flat_bars(15), period=14) == 10.0


class TestADX:
    def test_steady_uptrend_is_100(self) -> None:
        # Each bar shifts the whole range up by 1: +DM=1, -DM=0 every
        # bar, so +DI>0, -DI=0 -> DX=100 -> ADX=100.
        bars = [
            OHLCBar(
                symbol=_BTC,
                interval_minutes=60,
                opened_at=_T0 + timedelta(hours=i),
                open=Decimal(str(105 + i)),
                high=Decimal(str(110 + i)),
                low=Decimal(str(100 + i)),
                close=Decimal(str(109 + i)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
            for i in range(10)
        ]
        assert compute_adx(bars, period=3) == pytest.approx(100.0)

    def test_flat_market_is_0(self) -> None:
        # Identical bars: +DM = -DM = 0 -> DX 0 -> ADX 0.
        assert compute_adx(_flat_bars(30), period=3) == 0.0

    def test_too_short_returns_none(self) -> None:
        # Needs 2*period + 1 bars.
        assert compute_adx(_flat_bars(28), period=14) is None
        assert compute_adx(_flat_bars(29), period=14) is not None


class TestStochastic:
    def test_close_at_top_of_range_is_100(self) -> None:
        # Strictly rising closes: the latest close sits at the window
        # high minus the +1 bracket; use bars where close == high.
        bars = [
            OHLCBar(
                symbol=_BTC,
                interval_minutes=60,
                opened_at=_T0 + timedelta(hours=i),
                open=Decimal(str(100 + i)),
                high=Decimal(str(101 + i)),
                low=Decimal(str(99 + i)),
                close=Decimal(str(101 + i)),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
            for i in range(20)
        ]
        result = compute_stochastic(bars)
        assert result is not None
        assert result.k == 100.0

    def test_mid_range_close_is_50(self) -> None:
        result = compute_stochastic(_flat_bars(20))
        assert result is not None
        # Close sits mid-range of the constant [100, 110] window.
        assert result.k == 50.0
        assert result.d == 50.0

    def test_degenerate_zero_range_window_is_neutral_50(self) -> None:
        # Every bar high==low==close: highest==lowest, %K undefined ->
        # the documented 50-neutral convention.
        result = compute_stochastic(_flat_bars(20, low=100.0, high=100.0))
        assert result is not None
        assert result.k == 50.0

    def test_d_is_sma_of_k(self) -> None:
        closes = [float(100 + (i * 7) % 11) for i in range(30)]
        result = compute_stochastic(_bars_from_closes(closes))
        assert result is not None
        assert 0.0 <= result.k <= 100.0
        assert 0.0 <= result.d <= 100.0

    def test_too_short_returns_none(self) -> None:
        # Needs k_period + d_period - 1 = 16 bars.
        assert compute_stochastic(_flat_bars(15)) is None

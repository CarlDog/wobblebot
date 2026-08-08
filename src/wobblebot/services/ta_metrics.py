"""Technical-analysis indicators over OHLC bars (P2 slice 3).

The spine's computation layer: standard TA vocabulary (RSI, MACD,
Bollinger, SMA/EMA, ATR, ADX, Stochastic) computed from ``OHLCBar``
lists so the advisor sees the inputs the LLM's training distribution
expects — RSI(14) on bars, not a moving-average proxy on ticks.

Deliberately a NEW module, not ``services/metrics.py`` (per the P2
blueprint): ``metrics.py`` is the tick/snapshot layer; this is the
bar layer. Hand-rolled with standard formulas rather than a pandas/
TA-Lib dependency — eight indicators over plain lists don't earn a
heavyweight runtime dep, and hand-rolling keeps the math pinned by
our own tests (same posture as the screener's Pearson-from-scratch).

Contract (published by the 2026-06-03 blueprint):

- Every public ``compute_*`` takes ``bars`` sorted ASC by
  ``opened_at`` — exactly what ``StoragePort.get_ohlc_bars``
  returns — and returns the indicator's LATEST value, or ``None``
  when the input is too short for the requested period(s).
- Compound indicators return frozen ``MACDResult`` /
  ``BollingerResult`` / ``StochasticResult``.
- The private ``_compute_*_series`` helpers are internal iteration
  state only — nothing outside this module imports them. (The
  blueprint anticipated the auditor/screener consuming full series;
  both went other routes: the auditor replays through the real
  engine, the screener uses ``compute_atr``.)

**Advisor-only.** Nothing here is wired into ``cli/live`` decisions
(ADR-002); consumers are the advisor summary (``SummaryBuilder``, all
eight indicators) and the screener (``compute_atr``).

Note on ``vwap``: imported dump bars carry ``vwap=0`` (no vwap column
in Kraken's OHLCVT CSVs). No indicator in this module reads ``vwap``,
by design — do not add one without handling the 0-sentinel.
"""

from __future__ import annotations

from dataclasses import dataclass

from wobblebot.domain.value_objects import OHLCBar

# Wilder's smoothing (RSI/ATR/ADX) and the 12/26/9 + 20/2 + 14/3
# defaults below are THE textbook parameterizations — the whole point
# of this module is matching the vocabulary the LLM already knows, so
# deviating defaults would undercut it.

__all__ = (
    "BollingerResult",
    "MACDResult",
    "StochasticResult",
    "compute_adx",
    "compute_atr",
    "compute_bollinger",
    "compute_ema",
    "compute_macd",
    "compute_rsi",
    "compute_sma",
    "compute_stochastic",
)


@dataclass(frozen=True)
class MACDResult:
    """MACD line, signal line, and histogram (``line - signal``)."""

    line: float
    signal: float
    histogram: float


@dataclass(frozen=True)
class BollingerResult:
    """Bollinger bands: middle = SMA(period), upper/lower = ±num_std·σ."""

    upper: float
    middle: float
    lower: float


@dataclass(frozen=True)
class StochasticResult:
    """Stochastic oscillator: %K and its SMA smoothing %D."""

    k: float
    d: float


def _closes(bars: list[OHLCBar]) -> list[float]:
    return [float(ohlc.close) for ohlc in bars]


def _sma_of(values: list[float]) -> float:
    return sum(values) / len(values)


def compute_sma(bars: list[OHLCBar], period: int) -> float | None:
    """Simple moving average of the last ``period`` closes."""
    if period <= 0 or len(bars) < period:
        return None
    return _sma_of(_closes(bars)[-period:])


def _compute_ema_series(values: list[float], period: int) -> list[float]:
    """EMA series aligned to ``values[period - 1:]``.

    Standard seeding: the first EMA value is the SMA of the first
    ``period`` inputs; thereafter ``EMA_t = v_t·k + EMA_{t-1}·(1-k)``
    with ``k = 2 / (period + 1)``.
    """
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    series = [_sma_of(values[:period])]
    for value in values[period:]:
        series.append(value * k + series[-1] * (1.0 - k))
    return series


def compute_ema(bars: list[OHLCBar], period: int) -> float | None:
    """Exponential moving average (standard 2/(n+1) smoothing)."""
    series = _compute_ema_series(_closes(bars), period)
    return series[-1] if series else None


def _compute_rsi_series(closes: list[float], period: int) -> list[float]:
    """Wilder RSI series aligned to ``closes[period:]``."""
    if period <= 0 or len(closes) < period + 1:
        return []
    gains = []
    losses = []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = _sma_of(gains[:period])
    avg_loss = _sma_of(losses[:period])
    series = [_rsi_from_averages(avg_gain, avg_loss)]
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        series.append(_rsi_from_averages(avg_gain, avg_loss))
    return series


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def compute_rsi(bars: list[OHLCBar], period: int = 14) -> float | None:
    """Wilder RSI. 50.0 for a perfectly flat window (no gains, no losses)."""
    series = _compute_rsi_series(_closes(bars), period)
    return series[-1] if series else None


def _compute_macd_series(
    closes: list[float], fast: int, slow: int, signal: int
) -> tuple[list[float], list[float]]:
    """(macd_line, signal_line) series, both aligned to their common tail."""
    if fast >= slow:
        return [], []
    fast_series = _compute_ema_series(closes, fast)
    slow_series = _compute_ema_series(closes, slow)
    if not slow_series:
        return [], []
    # fast EMA starts (slow - fast) entries earlier; align tails.
    macd_line = [f - s for f, s in zip(fast_series[slow - fast :], slow_series)]
    signal_series = _compute_ema_series(macd_line, signal)
    if not signal_series:
        return [], []
    return macd_line[signal - 1 :], signal_series


def compute_macd(
    bars: list[OHLCBar], fast: int = 12, slow: int = 26, signal: int = 9
) -> MACDResult | None:
    """MACD(12,26,9): EMA(fast) − EMA(slow), EMA(signal) of that, histogram."""
    macd_line, signal_series = _compute_macd_series(_closes(bars), fast, slow, signal)
    if not macd_line:
        return None
    return MACDResult(
        line=macd_line[-1],
        signal=signal_series[-1],
        histogram=macd_line[-1] - signal_series[-1],
    )


def compute_bollinger(
    bars: list[OHLCBar], period: int = 20, num_std: float = 2.0
) -> BollingerResult | None:
    """Bollinger(20, 2σ) with population standard deviation (the standard)."""
    if period <= 0 or len(bars) < period:
        return None
    window = _closes(bars)[-period:]
    middle = _sma_of(window)
    variance = sum((value - middle) ** 2 for value in window) / period
    offset = num_std * variance**0.5
    return BollingerResult(upper=middle + offset, middle=middle, lower=middle - offset)


def _compute_tr_series(bars: list[OHLCBar]) -> list[float]:
    """True-range series aligned to ``bars[1:]`` (needs the prior close)."""
    series = []
    for prev, cur in zip(bars, bars[1:]):
        prev_close = float(prev.close)
        high = float(cur.high)
        low = float(cur.low)
        series.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return series


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder-smoothed series aligned to ``values[period - 1:]``."""
    if period <= 0 or len(values) < period:
        return []
    series = [_sma_of(values[:period])]
    for value in values[period:]:
        series.append((series[-1] * (period - 1) + value) / period)
    return series


def _compute_atr_series(bars: list[OHLCBar], period: int) -> list[float]:
    return _wilder_smooth(_compute_tr_series(bars), period)


def compute_atr(bars: list[OHLCBar], period: int = 14) -> float | None:
    """Wilder ATR — average true range in quote-currency units."""
    series = _compute_atr_series(bars, period)
    return series[-1] if series else None


def _directional_movements(bars: list[OHLCBar]) -> tuple[list[float], list[float]]:
    """(+DM, -DM) series aligned to ``bars[1:]``."""
    plus_dm = []
    minus_dm = []
    for prev, cur in zip(bars, bars[1:]):
        up_move = float(cur.high) - float(prev.high)
        down_move = float(prev.low) - float(cur.low)
        plus_dm.append(up_move if up_move > down_move and up_move > 0.0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0.0 else 0.0)
    return plus_dm, minus_dm


def _dx_value(tr_value: float, plus: float, minus: float) -> float:
    """One DX point from smoothed TR / +DM / -DM (0 on degenerate input)."""
    if tr_value == 0.0:
        return 0.0
    plus_di = 100.0 * plus / tr_value
    minus_di = 100.0 * minus / tr_value
    di_sum = plus_di + minus_di
    return 0.0 if di_sum == 0.0 else 100.0 * abs(plus_di - minus_di) / di_sum


def _compute_adx_series(bars: list[OHLCBar], period: int) -> list[float]:
    """Wilder ADX series. Needs ``2·period + 1`` bars for one value."""
    if period <= 0 or len(bars) < 2 * period + 1:
        return []
    plus_dm, minus_dm = _directional_movements(bars)
    tr_smooth = _wilder_smooth(_compute_tr_series(bars), period)
    plus_smooth = _wilder_smooth(plus_dm, period)
    minus_smooth = _wilder_smooth(minus_dm, period)
    dx_series = [
        _dx_value(tr_value, plus, minus)
        for tr_value, plus, minus in zip(tr_smooth, plus_smooth, minus_smooth)
    ]
    return _wilder_smooth(dx_series, period)


def compute_adx(bars: list[OHLCBar], period: int = 14) -> float | None:
    """Wilder ADX — trend strength 0..100 (ADR-019: trend is the dominant variable)."""
    series = _compute_adx_series(bars, period)
    return series[-1] if series else None


def _compute_stochastic_series(
    bars: list[OHLCBar], k_period: int, d_period: int
) -> tuple[list[float], list[float]]:
    """(%K, %D) series; %D aligned to the last ``len(K) - d_period + 1`` entries."""
    if k_period <= 0 or d_period <= 0 or len(bars) < k_period + d_period - 1:
        return [], []
    k_series = []
    for end in range(k_period, len(bars) + 1):
        window = bars[end - k_period : end]
        highest = max(float(ohlc.high) for ohlc in window)
        lowest = min(float(ohlc.low) for ohlc in window)
        close = float(window[-1].close)
        if highest == lowest:
            # Flat window — %K undefined; 50 is the neutral convention.
            k_series.append(50.0)
        else:
            k_series.append(100.0 * (close - lowest) / (highest - lowest))
    d_series = [_sma_of(k_series[i - d_period : i]) for i in range(d_period, len(k_series) + 1)]
    return k_series[d_period - 1 :], d_series


def compute_stochastic(
    bars: list[OHLCBar], k_period: int = 14, d_period: int = 3
) -> StochasticResult | None:
    """Stochastic(14, 3): %K position-in-range, %D = SMA(3) of %K."""
    k_series, d_series = _compute_stochastic_series(bars, k_period, d_period)
    if not k_series:
        return None
    return StochasticResult(k=k_series[-1], d=d_series[-1])

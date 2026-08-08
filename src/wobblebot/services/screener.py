"""Grid-suitability scoring for ``cli/screener`` (P2 slice 5).

Pure functions, no I/O — the CLI wires storage to these. Per the
ratified blueprint:

- **Rank-based composite**, not a weighted formula: each metric ranks
  the cohort 1..N and the composite is the mean rank (lower = better).
  Ranks sidestep the incommensurable-units problem a linear combo of
  raw vol/flatness/ATR would have.
- **Volatility and ATR%% are scored as distance from a band center** —
  NOT monotonically. Too quiet = the grid never cycles; too hot = the
  caps trip. Closest to the configured sweet spot ranks best.
- **Flatness ranks descending** — high flatness = ranging = a grid's
  ideal (same reading the quant prompt uses).
- **Correlation is a post-score ANNOTATION, never a rank factor**
  (blueprint decision): it tells the operator "this candidate moves
  with something you already hold," it does not demote the candidate.
  Pearson from scratch over aligned per-bar returns; ``None`` when the
  overlap is too thin to mean anything — expected for novel candidates
  until their history is imported/observed.

All metrics compute over ONE uniform source — bar closes at the
screener interval — so cross-symbol comparisons share a clock.
(The blueprint's older "price-snapshot vol/flatness" wording predates
the history import; snapshot cadence varies by source, which would
make volatility incomparable across symbols.)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.services.metrics import compute_flatness, compute_volatility
from wobblebot.services.ta_metrics import compute_atr

# Below this many bars the metrics are noise, not signal (ATR(14)
# alone wants 15; vol/flatness want a real window).
MIN_BARS = 30

# Correlation annotations need enough aligned return points to mean
# anything; below this the column reads "n/a".
MIN_CORRELATION_OVERLAP = 50

__all__ = (
    "MIN_BARS",
    "MIN_CORRELATION_OVERLAP",
    "ScreenerRanking",
    "SymbolMetrics",
    "bar_returns",
    "max_abs_correlation",
    "compute_symbol_metrics",
    "pearson",
    "rank_candidates",
)


@dataclass(frozen=True)
class SymbolMetrics:
    """One symbol's raw v1 suitability inputs."""

    symbol: Symbol
    volatility: float  # stdev of close-to-close returns per bar (fraction)
    flatness: float  # 1 - range/mean over the window, clamped [0, 1]
    atr_pct: float  # ATR(14) as % of the latest close
    bar_count: int


@dataclass(frozen=True)
class ScreenerRanking:
    """One symbol's ranked outcome plus the correlation annotation."""

    metrics: SymbolMetrics
    vol_rank: int
    flatness_rank: int
    atr_rank: int
    composite: float  # mean of the three ranks; lower = better
    max_correlation: float | None = None  # annotation only, never a factor
    correlated_with: Symbol | None = None


def compute_symbol_metrics(bars: list[OHLCBar]) -> SymbolMetrics | None:
    """Raw metrics from one symbol's ASC bar window; None when too thin."""
    if len(bars) < MIN_BARS:
        return None
    closes = [ohlc.close for ohlc in bars]
    atr = compute_atr(bars)
    if atr is None:
        return None
    latest_close = closes[-1]
    if latest_close == 0:
        return None
    return SymbolMetrics(
        symbol=bars[0].symbol,
        volatility=float(compute_volatility(closes)),
        flatness=float(compute_flatness(closes)),
        atr_pct=float(Decimal(str(atr)) / latest_close * 100),
        bar_count=len(bars),
    )


def _ranks(keyed: list[tuple[Symbol, float]]) -> dict[Symbol, int]:
    """1-based ranks after an ascending sort (ties broken by symbol for
    determinism)."""
    ordered = sorted(keyed, key=lambda pair: (pair[1], str(pair[0])))
    return {symbol: index + 1 for index, (symbol, _) in enumerate(ordered)}


def rank_candidates(
    metrics: list[SymbolMetrics],
    *,
    vol_band_center: float,
    atr_band_center_pct: float,
) -> list[ScreenerRanking]:
    """Rank the cohort; returns rankings sorted best-first.

    vol/ATR%% rank by ``|value - center|`` ascending (band distance);
    flatness ranks descending (negated for the ascending ranker).
    Correlation annotations are attached separately by the caller —
    they need cross-symbol return series this cohort math doesn't.
    """
    if not metrics:
        return []
    vol_ranks = _ranks([(m.symbol, abs(m.volatility - vol_band_center)) for m in metrics])
    atr_ranks = _ranks([(m.symbol, abs(m.atr_pct - atr_band_center_pct)) for m in metrics])
    flat_ranks = _ranks([(m.symbol, -m.flatness) for m in metrics])
    rankings = [
        ScreenerRanking(
            metrics=m,
            vol_rank=vol_ranks[m.symbol],
            flatness_rank=flat_ranks[m.symbol],
            atr_rank=atr_ranks[m.symbol],
            composite=(vol_ranks[m.symbol] + flat_ranks[m.symbol] + atr_ranks[m.symbol]) / 3.0,
        )
        for m in metrics
    ]
    return sorted(rankings, key=lambda r: (r.composite, str(r.metrics.symbol)))


def bar_returns(bars: list[OHLCBar]) -> dict[str, float]:
    """Per-bar close-to-close returns keyed by the bar's opened_at ISO string.

    The key is the alignment handle for cross-symbol correlation: two
    symbols' returns only pair up when their bars share an opened_at.
    """
    out: dict[str, float] = {}
    for prev, cur in zip(bars, bars[1:]):
        if prev.close == 0:
            continue
        out[cur.opened_at.isoformat()] = float((cur.close - prev.close) / prev.close)
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation from scratch; None on degenerate input."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return None
    return float(cov / (var_x * var_y) ** 0.5)


def max_abs_correlation(
    candidate_returns: dict[str, float],
    held_returns: dict[Symbol, dict[str, float]],
) -> tuple[float, Symbol] | None:
    """Strongest |Pearson| between a candidate and any held symbol.

    Returns ``None`` when no held symbol shares at least
    ``MIN_CORRELATION_OVERLAP`` aligned bars with the candidate — the
    honest "n/a" for thin/novel history.
    """
    best_r: float | None = None
    best_symbol: Symbol | None = None
    for held_symbol, returns in held_returns.items():
        shared = sorted(candidate_returns.keys() & returns.keys())
        if len(shared) < MIN_CORRELATION_OVERLAP:
            continue
        r = pearson(
            [candidate_returns[k] for k in shared],
            [returns[k] for k in shared],
        )
        if r is None:
            continue
        if best_r is None or abs(r) > abs(best_r):
            best_r, best_symbol = r, held_symbol
    if best_r is None or best_symbol is None:
        return None
    return best_r, best_symbol

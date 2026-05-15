"""Pure metrics math for the Stage 3.1 Data Collector v2.

No I/O, no port dependencies, no logging. Functions in this module
take already-loaded series (prices, trades) and return scalar or
small-aggregate metrics. ``DataCollector v2`` wires the storage
read path into these to expose derived metrics through
``DataCollectorPort``.

Stage 3.1 picks deliberately simple, well-known definitions; the
advisor work in Stage 3.2-3.4 will iterate on which metrics are
actually useful and may add new ones here. None of these compute on
streaming data — every call is a closed-form pass over the window
the caller hands in.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from statistics import stdev

from wobblebot.domain.models import CycleStats, Trade
from wobblebot.domain.value_objects import OrderSide

__all__ = [
    "CycleStats",
    "compute_cycle_stats",
    "compute_flatness",
    "compute_max_drawdown",
    "compute_volatility",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")


def compute_volatility(prices: Sequence[Decimal]) -> Decimal:
    """Sample standard deviation of simple returns.

    Returns the sample stdev (n-1 denominator) of the period-over-period
    fractional returns ``(p[i] - p[i-1]) / p[i-1]``. Scale-invariant —
    comparable across symbols regardless of price magnitude.

    Args:
        prices: Sequential prices, oldest first. Must be positive.

    Returns:
        Sample stdev as a Decimal. ``Decimal("0")`` when fewer than two
        prices are supplied — the caller can distinguish "no signal" from
        "computed zero" by checking the input length themselves.

    Raises:
        ValueError: If any price is non-positive (return is undefined).
    """
    if any(p <= 0 for p in prices):
        raise ValueError("All prices must be positive for volatility computation")
    if len(prices) < 2:
        return _ZERO
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    if len(returns) < 2:
        # Single-return sample has no defined sample stdev (n-1 = 0).
        return _ZERO
    return Decimal(stdev(returns))


def compute_max_drawdown(prices: Sequence[Decimal]) -> Decimal:
    """Worst peak-to-trough decline as a fraction.

    Walks the series tracking the running maximum; at each point computes
    ``(price - running_max) / running_max`` and returns the minimum
    (most negative) value seen. Always ``<= 0``.

    Args:
        prices: Sequential prices, oldest first. Must be positive.

    Returns:
        Maximum drawdown as a non-positive Decimal. ``Decimal("0")`` when
        the series is empty, has one element, or monotonically rises.

    Raises:
        ValueError: If any price is non-positive.
    """
    if any(p <= 0 for p in prices):
        raise ValueError("All prices must be positive for drawdown computation")
    if len(prices) < 2:
        return _ZERO
    peak = prices[0]
    worst = _ZERO
    for p in prices[1:]:
        if p > peak:
            peak = p
        else:
            worst = min(worst, (p - peak) / peak)
    return worst


def compute_flatness(prices: Sequence[Decimal]) -> Decimal:
    """How tightly the price clings to its mean over the window.

    Defined as ``1 - (max - min) / mean``. Higher = flatter:

    - ``1.0`` — constant price (zero range).
    - ``0.0`` — range equals the mean.
    - Clamped at ``0.0`` for highly volatile windows where range exceeds
      the mean (the negative-flatness regime isn't useful to expose).

    Args:
        prices: Prices over the window. Order doesn't matter (uses
            max/min/mean). Must be positive.

    Returns:
        Flatness in ``[0, 1]``. ``Decimal("1")`` when fewer than two
        prices are supplied (a single point is trivially "flat").

    Raises:
        ValueError: If any price is non-positive.
    """
    if any(p <= 0 for p in prices):
        raise ValueError("All prices must be positive for flatness computation")
    if len(prices) < 2:
        return _ONE
    high = max(prices)
    low = min(prices)
    mean = sum(prices, _ZERO) / Decimal(len(prices))
    flatness = _ONE - (high - low) / mean
    return max(flatness, _ZERO)


def compute_cycle_stats(trades: Sequence[Trade]) -> CycleStats:
    """FIFO-match buys to sells per symbol; compute aggregate stats.

    For each symbol independently, a buy enters a queue. A sell pops
    the oldest queued buy on the same symbol and forms a cycle. Sells
    with no matching buy in the queue are skipped (interpretation:
    selling pre-existing position).

    Amounts aren't required to match exactly — grid trading produces
    cycles where the buy and sell amounts can drift slightly with
    price (uniform ``order_size_usd`` budget). PnL is cost-based, so
    amount divergence is already captured in the ``cost`` fields.

    Args:
        trades: Trades in chronological order (oldest first). Not
            re-sorted internally; consumers should pass a
            time-ordered window.

    Returns:
        Aggregated ``CycleStats``. All zeros when no cycles can be
        matched (empty input, only buys, only sells, or unmatched
        sequences).
    """
    queues: dict[tuple[str, str], list[Trade]] = {}
    cycles: list[Decimal] = []
    for trade in trades:
        key = (trade.symbol.base, trade.symbol.quote)
        queue = queues.setdefault(key, [])
        if trade.side == OrderSide.BUY:
            queue.append(trade)
        else:
            if not queue:
                continue
            buy = queue.pop(0)
            pnl = trade.cost - buy.cost - buy.fee - trade.fee
            cycles.append(pnl)
    cycle_count = len(cycles)
    if cycle_count == 0:
        return CycleStats(
            cycle_count=0,
            win_count=0,
            win_rate=_ZERO,
            total_pnl=_ZERO,
            avg_profit_per_cycle=_ZERO,
        )
    win_count = sum(1 for pnl in cycles if pnl > 0)
    total_pnl = sum(cycles, _ZERO)
    return CycleStats(
        cycle_count=cycle_count,
        win_count=win_count,
        win_rate=Decimal(win_count) / Decimal(cycle_count),
        total_pnl=total_pnl,
        avg_profit_per_cycle=total_pnl / Decimal(cycle_count),
    )

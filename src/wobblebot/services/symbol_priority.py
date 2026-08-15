"""Order the per-tick symbol sweep by likelihood of completing a cycle.

**Why this exists.** ``cli/live`` swept ``live.symbols`` in config order,
so the first-listed symbol claimed the exposure and daily-spend caps
before any other symbol was asked. Measured in production 2026-08-15,
the starvation gradient was exact and monotonic down the config order:

    ETH (2nd)  placed 5/6
    SOL (3rd)  placed 2/6   (4 refused)
    ADA (6th)  placed 0/6   (3 refused, 3 sells deferred -> back-off)

BTC, listed first, had first claim on every tick for months.

**What ordering can and cannot do.** It redistributes scarcity; it does
not create capacity. Six symbols x 6 levels x $5 is $180 of two-sided
grid demand against ~$13 of free USD — something starves regardless. The
only question worth answering is *which* symbol should starve, and the
answer is the one least likely to complete a round trip.

**The key.** Primary is the screener's composite (``services/screener``),
which ranks the cohort on volatility and ATR% as DISTANCE FROM A BAND
CENTRE — deliberately not monotonic, because too quiet never cycles and
too hot trips the caps — plus flatness descending. That is already a
grid-suitability score, so this module reuses it rather than inventing a
second, divergent notion of "good to trade".

The tiebreak is PROXIMITY: how far the current price sits from the
nearest grid level, measured in ATR. Composite is a mean of three
integer ranks, so its values are discrete (k/3) and exact ties are
common in a six-symbol cohort — the tiebreak really fires rather than
being decorative. Screener rank answers "is this a good grid candidate
at all"; proximity answers "is it about to fill". Character first,
timing second.

Pure functions, no I/O — the caller supplies prices, grid levels and
metrics. Deterministic: the final key is the symbol name, so an
unchanged cohort always produces an unchanged order.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

from wobblebot.domain.value_objects import Symbol
from wobblebot.services.screener import ScreenerRanking

__all__ = ["order_symbols", "proximity_in_atr"]


def proximity_in_atr(
    price: Decimal | None,
    level_prices: Sequence[Decimal],
    atr_absolute: float | None,
) -> float:
    """Distance from ``price`` to the nearest level, in ATR units.

    Lower is closer to a fill. Returns ``inf`` when the answer is
    unknowable (no price, no levels, or no usable ATR) so those symbols
    sort LAST — an unknown is not an opportunity, and the caller should
    never be able to mistake missing data for imminence.

    ATR normalisation is what makes symbols of wildly different price
    scales comparable: BTC 300 dollars from a level and DOGE 0.003 from
    one are the same distance in the only unit that predicts a fill.
    """
    if price is None or not level_prices:
        return math.inf
    if atr_absolute is None or atr_absolute <= 0:
        return math.inf
    nearest = min(abs(float(price) - float(level)) for level in level_prices)
    return nearest / atr_absolute


def order_symbols(
    configured: Sequence[Symbol],
    rankings: Sequence[ScreenerRanking],
    proximity: dict[Symbol, float],
) -> list[Symbol]:
    """Sweep order: screener composite, then proximity, then name.

    Every configured symbol is returned exactly once — this decides
    ORDER, never membership. A symbol the screener could not rank (too
    few bars, no ATR) keeps its place in the cohort but sorts after every
    ranked one: unranked means unknown, and unknown must not outrank
    measured suitability.

    Paused / offside / disabled symbols are deliberately NOT filtered.
    They return early from ``GridEngine.step`` without placing anything,
    so they consume no budget and cost nothing but a no-op — filtering
    them here would add a second, drifting notion of "active" alongside
    the engine's own.
    """
    by_symbol = {r.metrics.symbol: r for r in rankings}

    def key(symbol: Symbol) -> tuple[int, float, float, str]:
        ranking = by_symbol.get(symbol)
        if ranking is None:
            # Unranked: sorts after everything ranked, then by name.
            return (1, 0.0, 0.0, str(symbol))
        return (
            0,
            ranking.composite,
            proximity.get(symbol, math.inf),
            str(symbol),
        )

    return sorted(configured, key=key)

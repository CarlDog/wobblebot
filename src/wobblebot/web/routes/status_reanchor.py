"""Re-anchor banner: recommendation DTO, severity tiering, loaders.

Split out of ``status.py`` when that module crossed the project's
1000-line pylint gate. The banner is the natural seam: it is one
cohesive question — *should the operator re-anchor this symbol, and
is it worth it?* — with its own DTO, thresholds, and storage reads,
and nothing else in the status page depends on its internals.

**Banners annotate; they never suppress.** Both viability stats
(``recent_range_spacings``, ``atr_spacings_per_hour``) can say "a
re-anchored grid probably wouldn't cycle here" — and neither one hides
or downgrades a banner when it does. Two reasons. First, "re-anchoring
isn't worth it" and "the current grid is fine" are different states: a
misplaced ladder in a dead market is still capital sitting idle, and
the right response may be *pause this symbol*, not *leave it drifted
and say nothing*. Second, per the advisor philosophy (ADR-002,
transparent guardrail), a heuristic that silently withholds
information the operator would have acted on is exactly the failure
mode this project designs against. Show the number; let the operator
weigh it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from wobblebot.config.grid import KRAKEN_TAKER_FEE_RATE
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.ta_metrics import compute_atr

_LOGGER = logging.getLogger(__name__)

ReanchorSeverity = Literal["mild", "moderate", "strong"]

# Re-anchor viability annotation (P3 slice 22). Hourly bars because
# that's the interval cli/observe's top-up maintains; 14 days gives
# Wilder ATR(14) a comfortable window (336 bars) while staying recent
# enough to describe the market the operator is deciding about.
_VIABILITY_INTERVAL_MINUTES = 60
_VIABILITY_LOOKBACK_DAYS = 14

# Re-anchor banner thresholds. Drift is the gate (no drift = no
# banner, even if grid is stale); age can escalate severity but
# can't trigger alone. Calm-market parked grids don't get scary
# banners; misaligned grids do.
_DRIFT_MILD_SPACINGS = 1.5
_DRIFT_MODERATE_SPACINGS = 2.5
_DRIFT_STRONG_SPACINGS = 4.0
_AGE_MILD_HOURS = 24
_AGE_MODERATE_HOURS = 48
_AGE_STRONG_HOURS = 72


@dataclass(frozen=True)
class ReanchorRecommendation:  # pylint: disable=too-many-instance-attributes
    # Display DTO for the banner — the attribute count grows with the
    # decision facts it carries (same posture as StatusSnapshot).
    """Per-symbol recommendation that the operator consider re-anchoring.

    Rendered as a colored banner on the dashboard. Severity tier
    drives the color: mild (yellow) -> moderate (orange) ->
    strong (red). The banner carries the Re-anchor action button
    (through pending_commands) + Snooze (UI-local) per the P3
    blueprint; ``projected_fee_usd`` is the fee-only decision
    economics line (paper-loss-on-stranded-inventory was rejected
    as misleading — cancelling doesn't sell the asset).
    """

    symbol: Symbol
    severity: ReanchorSeverity
    drift_in_spacings: float
    oldest_order_age_seconds: int
    current_price: Decimal
    anchor_price: Decimal
    # Fee-only projected cost of acting on the banner: taker rate on
    # the cancelled ladder's notional + the same again for the re-laid
    # ladder (approximated as equal). Cancels themselves are free on
    # Kraken — this is the ceiling on fees the rotation itself commits
    # you to, deliberately excluding unrealized inventory swings.
    projected_fee_usd: Decimal = Decimal("0")
    # Recent price range (sparkline window) expressed in grid spacings —
    # the honest activity stat (operator-requested 2026-08-09): well
    # under 1x means the market isn't moving enough to cycle even a
    # correctly-placed grid, so a re-anchor would buy an idle ladder.
    # Deliberately a FACT, not a probability claim — the operator does
    # the weighting. None when the price series is too thin.
    recent_range_spacings: float | None = None
    # Longer-window companion to the 2h stat (P3 slice 22, the full
    # viability item): Wilder ATR(14) over stored HOURLY bars, divided
    # by grid spacing. "0.3x" = in a typical hour price travels a third
    # of a spacing, so a fill takes ~3 hours on average even from a
    # perfectly-placed ladder. The 2h stat can be a quiet-window fluke;
    # this one is ~2 weeks of behavior. Also a FACT, never a suppressor
    # — a low value NEVER hides or downgrades a banner (see the module
    # note on why "not worth re-anchoring" and "fine as-is" are
    # different states). None when stored bars are too thin.
    atr_spacings_per_hour: float | None = None


def _classify_reanchor_severity(drift_spacings: float, age_seconds: int) -> ReanchorSeverity | None:
    """Return severity tier, or ``None`` if no banner should show.

    Drift is the gate: without meaningful drift the engine isn't
    misaligned, so a banner would be noise even if the grid has
    been sitting for days. Age escalates severity but doesn't
    trigger alone — a calm market with a parked grid is normal.
    """
    if drift_spacings < _DRIFT_MILD_SPACINGS:
        return None
    if drift_spacings < _DRIFT_MODERATE_SPACINGS:
        drift_tier = 1
    elif drift_spacings < _DRIFT_STRONG_SPACINGS:
        drift_tier = 2
    else:
        drift_tier = 3
    age_hours = age_seconds / 3600
    if age_hours < _AGE_MILD_HOURS:
        age_tier = 0
    elif age_hours < _AGE_MODERATE_HOURS:
        age_tier = 1
    elif age_hours < _AGE_STRONG_HOURS:
        age_tier = 2
    else:
        age_tier = 3
    tier = max(drift_tier, age_tier)
    if tier == 1:
        return "mild"
    if tier == 2:
        return "moderate"
    return "strong"


async def load_reanchor_snoozes(
    operator_storage: StoragePort | None,
    *,
    now: datetime,
) -> set[Symbol]:
    """Symbols with an UNEXPIRED banner snooze; degrades to empty.

    Storage failure or an unwired operator.db degrade to "nothing
    snoozed" — the worst case is a banner the operator asked to
    hide reappearing, never a hidden recommendation.
    """
    if operator_storage is None:
        return set()
    try:
        snoozes = await operator_storage.get_reanchor_snoozes()
    except StorageError as exc:
        _LOGGER.warning(
            "reanchor-snooze lookup failed; showing all banners: %s",
            exc,
            extra={"error": str(exc)},
        )
        return set()
    return {symbol for symbol, until in snoozes.items() if until > now}


async def _load_atr_spacings(
    observe_storage: StoragePort | None,
    symbol: Symbol,
    spacing: Decimal,
) -> float | None:
    """Wilder ATR(14) over stored hourly bars, in units of grid spacing.

    Answers the question drift+age can't: *would a correctly-placed
    grid here actually cycle?* A value near or above 1 means a typical
    hour moves price a full spacing, so a fresh ladder should fill; a
    value well under 1 means it would sit idle — the 2026-08-09
    observation where an operator-executed BTC re-anchor produced
    correctly-positioned orders that nothing touched.

    Hourly bars, not daily: they're what `cli/observe`'s top-up
    actually maintains, and ATR doesn't rescale across intervals by
    any honest constant. Expressing it per-hour keeps the number
    literal — "a third of a spacing an hour" needs no conversion to
    act on.

    Degrades to ``None`` (banner renders "—") on an unwired
    observe.db, a thin bar window, or a storage failure. This is an
    annotation; it must never be able to break the banner it annotates.
    """
    if observe_storage is None or spacing <= 0:
        return None
    start = datetime.now(UTC) - timedelta(days=_VIABILITY_LOOKBACK_DAYS)
    try:
        bars = await observe_storage.get_ohlc_bars(
            symbol, _VIABILITY_INTERVAL_MINUTES, start_time=start
        )
    except StorageError as exc:
        _LOGGER.warning(
            "viability ATR lookup failed for %s; banner renders without it: %s",
            symbol,
            exc,
            extra={"symbol": str(symbol), "error": str(exc)},
        )
        return None
    atr = compute_atr(bars)
    if atr is None:
        return None
    return float(Decimal(str(atr)) / spacing)


async def load_reanchor_recommendations(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    # too-many-arguments: the loader consumes the snapshot's already-
    # fetched slices (orders, prices, ages, snoozes, series) rather
    # than re-querying storage — each param saves a duplicate query.
    live_storage: StoragePort,
    open_orders: list[Order],
    current_prices: dict[Symbol, Decimal],
    order_ages: dict[str, int],
    snoozed: set[Symbol],
    price_series: dict[Symbol, list[Decimal]],
    observe_storage: StoragePort | None = None,
) -> tuple[ReanchorRecommendation, ...]:
    """Per-symbol re-anchor recommendations from drift + age heuristic.

    Drift = distance from current price to the nearest open order,
    expressed in units of grid spacing. Age = oldest open order
    for that symbol. ``_classify_reanchor_severity`` gates and
    tiers; symbols in ``snoozed`` are suppressed entirely;
    per-symbol storage failures are logged + skipped.
    ``price_series`` (the sparkline window) feeds the banner's
    activity stat — recent range in spacings.
    """
    if not open_orders:
        return ()
    symbols = {o.symbol for o in open_orders}
    recommendations: list[ReanchorRecommendation] = []
    for symbol in symbols:
        if symbol in snoozed:
            continue
        current = current_prices.get(symbol)
        if current is None:
            continue
        try:
            state = await live_storage.get_grid_state(symbol)
        except StorageError as exc:
            _LOGGER.warning(
                "grid_state lookup failed for %s; skipping reanchor calc",
                symbol,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        if state is None:
            continue
        spacing = state.reference_price * state.spacing_percentage / Decimal("100")
        if spacing <= 0:
            continue
        symbol_orders = [o for o in open_orders if o.symbol == symbol]
        if not symbol_orders:
            continue
        nearest_distance = min(abs(o.price.amount - current) for o in symbol_orders)
        drift = float(nearest_distance / spacing)
        oldest_age = max(order_ages.get(str(o.id), 0) for o in symbol_orders)
        severity = _classify_reanchor_severity(drift, oldest_age)
        if severity is None:
            continue
        # Fee-only decision economics (P3 blueprint): taker rate on the
        # cancelled ladder's notional, plus the same again for the
        # re-laid ladder (approximated as equal to the current one).
        open_notional = sum((o.price.amount * o.amount.value for o in symbol_orders), Decimal(0))
        projected_fee = open_notional * KRAKEN_TAKER_FEE_RATE * Decimal(2)
        series = price_series.get(symbol)
        recent_range = float((max(series) - min(series)) / spacing) if series else None
        # Fetched AFTER the severity gate on purpose: the dashboard
        # polls every 15s, and banners are usually 0-2 symbols out of
        # six. Hoisting this above the gate would turn a rare, bounded
        # read into six queries a poll, forever.
        atr_spacings = await _load_atr_spacings(observe_storage, symbol, spacing)
        recommendations.append(
            ReanchorRecommendation(
                symbol=symbol,
                severity=severity,
                drift_in_spacings=drift,
                oldest_order_age_seconds=oldest_age,
                current_price=current,
                anchor_price=state.reference_price,
                projected_fee_usd=projected_fee,
                recent_range_spacings=recent_range,
                atr_spacings_per_hour=atr_spacings,
            )
        )
    return tuple(recommendations)


__all__ = (
    "ReanchorRecommendation",
    "ReanchorSeverity",
    "load_reanchor_recommendations",
    "load_reanchor_snoozes",
)

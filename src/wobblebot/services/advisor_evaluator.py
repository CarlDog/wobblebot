"""Advisor outcome evaluator — pure logic for ADR-035 counterfactual scoring (P4.2).

Everything here is deterministic, replay-free logic: classifying a
suggestion as scoreable / unscoreable (decision 5 — an unscoreable row
carries its reason, never a silent neutral), building the two
counterfactual ``GridConfig`` arms, choosing the fee schedule for the
window, and turning two replay results into the outcome SIGN
(``better`` / ``worse`` / ``tie`` — never dollars, decision 3).

The replay itself lives with the ADR-028 auditor
(``tools/auditor.py``); the batch driver that wires this module to the
auditor and the two databases is ``tools/score_recommendations.py``.
Keeping the replay out of this module keeps it adapter-free (hexagonal
rules: services depend on ports + domain + config only).

Evaluator version 1 semantics, pinned so a change forces a version bump
(re-scoring appends rows at the new version; old rows are audit
history):

- Kind is ROLE-gated (P4.4b): suggestions from ``DIRECTIONAL_ROLES``
  are ``directional_call`` — graded against the realized move over
  their own stated horizon (no replay, no arms, NULL granularity);
  everything else is a ``config_rec``, classified exactly as v1 did
  (pinned by test, which is why this stayed evaluator version 1).
- The in-force arm comes from the suggestion's own
  ``input_summary.current_grid`` (ratified 2026-08-17); the proposed
  arm is that config with the recommendation's replayable keys applied.
- Replay fees follow the schedule in force at ``window_start`` (Kraken
  doubled Tier-1 2026-07-09); a window straddling the cutover replays
  entirely at its start-of-window rates. The spacing-vs-fees floor is
  enforced HERE at the window's maker rate — not through
  ``GridConfig``'s validator, which hardcodes today's constant. The
  first live probe (2026-08-17) showed why: the May-era heuristic's
  0.65% first-order curve recs were legal above that era's 0.5% floor,
  and judging them by today's 0.8% floor would censor the corpus's most
  opinionated stretch as "invalid config". Same formula, window rate;
  arms therefore build via ``model_construct`` (the one sanctioned
  bypass of the config-load validator, greppable as such).
- Safety config is the operator's CURRENT settings with
  ``max_daily_spend_usd`` neutered in BOTH arms (ADR-028 correction 1);
  identical across arms, so it cancels out of the sign.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import ValidationError

from wobblebot.config.grid import (
    KRAKEN_MAKER_FEE_RATE,
    KRAKEN_TAKER_FEE_RATE,
    GridConfig,
    GridLevels,
)
from wobblebot.domain.value_objects import OHLCBar, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorSuggestion, RecommendationOutcome

EVALUATOR_VERSION = 1

# Ratified 2026-08-17 (docs/planning/p4-outcome-ledger-design.md): the
# forward window each score covers, recorded on every row so window
# variants can coexist later.
REPLAY_WINDOW = timedelta(days=7)

# The auditor can only replay what GridLevels expresses numerically.
# counter_target_mode is non-numeric and excluded by construction —
# the same boundary the auto-apply gate draws (ADR-029).
REPLAYABLE_KEYS = frozenset(
    {"spacing_percentage", "levels_above", "levels_below", "order_size_usd"}
)

# |proposed - in-force| at or below this is a tie. One cent: the replay
# is directional (ADR-028), so a sub-cent delta is arithmetic noise,
# not signal — but anything above it counts, because at $10 orders the
# real per-cycle margins are themselves cents.
TIE_EPSILON_USD = Decimal("0.01")

# Kraken doubled the Tier-1 spot schedule effective 2026-07-09
# (ADR-038). Windows starting before the cutover replay at the retired
# rates so pre-July recommendations are judged under the fees they were
# made against.
FEE_SCHEDULE_CUTOVER = datetime(2026, 7, 9, tzinfo=UTC)
PRE_CUTOVER_MAKER_FEE_RATE = Decimal("0.0025")
PRE_CUTOVER_TAKER_FEE_RATE = Decimal("0.0040")

# Bar-coverage floor: below this fraction of the window's expected bar
# count the replay would score a different (shorter) market than the
# window claims — unscoreable, with the counts in the reason.
MIN_BAR_COVERAGE = 0.9
# ... and the window's edges must be covered too: a head gap moves the
# anchor (correction 3 anchors at bar-0 open), a tail gap truncates
# exactly the part of the window the outcome depends on most.
_MAX_EDGE_GAP_BARS = 2

_GRID_FIELDS = ("spacing_percentage", "levels_above", "levels_below", "order_size_usd")
_INT_FIELDS = frozenset({"levels_above", "levels_below"})

# --- Directional calls (P4.4b, ADR-035 decision 4) -------------------
# Classification is ROLE-gated, not shape-gated: only roles registered
# here emit directional calls. A config role hallucinating a
# "direction" key keeps v1's exact treatment (unscoreable, "keys
# outside the replayable surface") — pinned by test, which is why
# EVALUATOR_VERSION stays 1: no existing-corpus row classifies
# differently.
DIRECTIONAL_ROLES = frozenset({"gremlin"})
DIRECTIONAL_DIRECTIONS = frozenset({"up", "down", "chop"})

# Realized |move| at or below this fraction over the horizon is
# "chop". Grading: an up/down call is right beyond the band the called
# way, wrong beyond it the other way, and a TIE inside it (the market
# didn't rule); a chop call is right inside the band, wrong outside —
# no tie case. Changing this band changes every future grade =
# EVALUATOR_VERSION bump.
DIRECTIONAL_CHOP_BAND = Decimal("0.01")

# Directional grades read last-known closes from bars at this
# interval; the outcome row's granularity_minutes stays NULL (the
# schema's directional marker) and the interval is recorded in the
# grading record instead.
DIRECTIONAL_GRADING_INTERVAL_MINUTES = 60


class ArmBuildError(Exception):
    """An arm could not be built; ``str(exc)`` is the unscoreable reason."""


@dataclass(frozen=True)
class Classification:
    """Replay-free triage of one suggestion.

    ``unscoreable_reason`` is ``None`` when the suggestion passed every
    check this stage can make (arm construction and bar coverage can
    still declare it unscoreable later). ``symbol`` is ``None`` exactly
    when the reason says the symbol was missing/unparseable.
    """

    kind: Literal["config_rec", "directional_call"]
    symbol: Symbol | None
    window_start: datetime
    window_end: datetime
    unscoreable_reason: str | None


def _parse_symbol(suggestion: AdvisorSuggestion) -> tuple[Symbol | None, str | None]:
    """(symbol, reason) — reason set exactly when symbol is None."""
    raw_symbol = suggestion.input_summary.get("symbol")
    if not isinstance(raw_symbol, str) or not raw_symbol:
        return None, "input_summary has no symbol"
    try:
        return Symbol.from_string(raw_symbol), None
    except ValueError:
        return None, f"unparseable symbol {raw_symbol!r}"


def _classify_directional(suggestion: AdvisorSuggestion) -> Classification:
    """Triage one directional call (a DIRECTIONAL_ROLES emission).

    The call carries its OWN horizon — ``window_end`` is
    ``created_at + horizon_hours``, not the 7-day config window. A
    malformed call gets a zero-length window so it surfaces as an
    unscoreable row immediately instead of sitting "pending" behind a
    horizon that will never grade.
    """
    window_start = suggestion.created_at.dt
    symbol, reason = _parse_symbol(suggestion)
    recs = suggestion.recommendation.recommendations
    horizon: float | None = None
    if reason is None:
        direction = recs.get("direction")
        raw_horizon = recs.get("horizon_hours")
        if direction not in DIRECTIONAL_DIRECTIONS:
            reason = (
                f"malformed directional call: direction {direction!r} "
                f"(expected one of {sorted(DIRECTIONAL_DIRECTIONS)})"
            )
        elif (
            isinstance(raw_horizon, bool)
            or not isinstance(raw_horizon, (int, float))
            or raw_horizon <= 0
        ):
            reason = f"malformed directional call: horizon_hours {raw_horizon!r}"
        else:
            horizon = float(raw_horizon)
    window_end = window_start + timedelta(hours=horizon) if horizon is not None else window_start
    return Classification(
        kind="directional_call",
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        unscoreable_reason=reason,
    )


def classify(suggestion: AdvisorSuggestion) -> Classification:
    """Triage a suggestion: kind, window, symbol, and scoreability."""
    if suggestion.recommendation.role in DIRECTIONAL_ROLES:
        return _classify_directional(suggestion)

    window_start = suggestion.created_at.dt
    window_end = window_start + REPLAY_WINDOW
    symbol, reason = _parse_symbol(suggestion)

    if reason is None:
        keys = suggestion.recommendation.recommendations
        if not keys:
            reason = "empty recommendation (no proposed change to score against)"
        else:
            foreign = sorted(set(keys) - REPLAYABLE_KEYS)
            if foreign:
                reason = f"keys outside the replayable surface: {', '.join(foreign)}"

    return Classification(
        kind="config_rec",
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        unscoreable_reason=reason,
    )


def fee_rates_for(window_start: datetime) -> tuple[Decimal, Decimal]:
    """(maker, taker) rates in force when the window opened."""
    if window_start < FEE_SCHEDULE_CUTOVER:
        return PRE_CUTOVER_MAKER_FEE_RATE, PRE_CUTOVER_TAKER_FEE_RATE
    return KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE


def _coerce(key: str, value: Any) -> Decimal | int:
    """JSON value -> the GridLevels field type, or ArmBuildError.

    ``bool`` is an ``int`` subclass in Python — reject it explicitly or
    ``True`` would coerce to a level count of 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArmBuildError(f"non-numeric value for {key!r}: {value!r}")
    if key in _INT_FIELDS:
        if isinstance(value, float) and not value.is_integer():
            raise ArmBuildError(f"non-integer level count for {key!r}: {value!r}")
        return int(value)
    return Decimal(str(value))


def _validation_reason(prefix: str, exc: ValidationError) -> str:
    """Compact one-line reason from a pydantic ValidationError."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "config"
    message = str(first["msg"]).replace("\n", " ")
    return f"{prefix}: {location}: {message}"


def _build_config(
    fields: dict[str, Decimal | int], *, maker_fee_rate: Decimal, label: str
) -> GridConfig:
    """One replay arm, validated for the WINDOW's fee schedule.

    ``GridLevels`` field validation runs in full. The spacing-vs-fees
    floor (``GridConfig``'s model validator) is re-applied here with
    the window's maker rate instead of today's constant — same formula,
    period-correct rate — so a pre-doubling recommendation is judged
    against the floor it was made under. ``model_construct`` skips ONLY
    that today-constant validator; it is the sanctioned bypass and this
    is its one call site.
    """
    try:
        levels = GridLevels(**fields)
    except ValidationError as exc:
        raise ArmBuildError(_validation_reason(f"{label} config invalid", exc)) from exc
    floor_pct = (maker_fee_rate * Decimal("2") * Decimal("100")).normalize()
    if levels.spacing_percentage <= floor_pct:
        raise ArmBuildError(
            f"{label} spacing {levels.spacing_percentage}% is at or below the "
            f"window's fee floor {floor_pct}% (2 x maker {(maker_fee_rate * 100).normalize()}%)"
        )
    return GridConfig.model_construct(default=levels, coins={})


def build_arms(
    suggestion: AdvisorSuggestion, *, maker_fee_rate: Decimal
) -> tuple[GridConfig, GridConfig]:
    """Build (in-force, proposed) replay configs for a scoreable suggestion.

    The in-force arm is ``input_summary.current_grid`` verbatim; the
    proposed arm re-validates the merged field set through the real
    ``GridLevels`` (never ``model_copy``, which skips validation), so an
    unbuildable proposal — negative size, spacing at or below the
    window's fee floor — surfaces as :class:`ArmBuildError` with the
    reason. ``maker_fee_rate`` must be the WINDOW's rate
    (:func:`fee_rates_for`), so the floor matches the fees the replay
    will charge.

    Raises:
        ArmBuildError: missing/incomplete ``current_grid``, non-numeric
            values, or either arm failing validation.
    """
    raw = suggestion.input_summary.get("current_grid")
    if not isinstance(raw, dict):
        raise ArmBuildError("input_summary has no current_grid")
    fields: dict[str, Decimal | int] = {}
    for key in _GRID_FIELDS:
        value = raw.get(key)
        if value is None:
            raise ArmBuildError(f"incomplete current_grid: {key} is missing/null")
        fields[key] = _coerce(key, value)
    inforce = _build_config(fields, maker_fee_rate=maker_fee_rate, label="in-force")

    updates = {
        key: _coerce(key, value)
        for key, value in suggestion.recommendation.recommendations.items()
        if key in REPLAYABLE_KEYS
    }
    proposed = _build_config({**fields, **updates}, maker_fee_rate=maker_fee_rate, label="proposed")
    return inforce, proposed


def bar_coverage_reason(
    bars: list[OHLCBar],
    *,
    window_start: datetime,
    window_end: datetime,
    interval_minutes: int,
) -> str | None:
    """``None`` when the bars honestly cover the window, else the reason."""
    if not bars:
        return f"no {interval_minutes}m bars in window"
    interval = timedelta(minutes=interval_minutes)
    expected = int((window_end - window_start) / interval)
    required = max(1, math.ceil(expected * MIN_BAR_COVERAGE))
    if len(bars) < required:
        return f"insufficient bars: {len(bars)}/{expected} at {interval_minutes}m"
    edge_gap = _MAX_EDGE_GAP_BARS * interval
    if bars[0].opened_at > window_start + edge_gap:
        return f"bar history starts late: first bar {bars[0].opened_at.isoformat()}"
    if bars[-1].opened_at < window_end - edge_gap:
        return f"bar history ends early: last bar {bars[-1].opened_at.isoformat()}"
    return None


def outcome_sign(
    proposed_net_pnl: Decimal, inforce_net_pnl: Decimal
) -> Literal["better", "worse", "tie"]:
    """The scoreboard's atom: the sign of the arms' difference."""
    delta = proposed_net_pnl - inforce_net_pnl
    if abs(delta) <= TIE_EPSILON_USD:
        return "tie"
    return "better" if delta > 0 else "worse"


def grade_directional(  # pylint: disable=too-many-return-statements
    direction: str, start_price: Decimal, end_price: Decimal
) -> Literal["better", "worse", "tie"]:
    """Grade one directional call against the realized move.

    ``better`` = the call was right, ``worse`` = wrong, ``tie`` = the
    market stayed inside the chop band so an up/down call was neither
    confirmed nor refuted. A ``chop`` call has no tie case — any move
    beyond the band falsifies it, which is the falsifiability the
    Gremlin design demands.
    """
    move = (end_price - start_price) / start_price
    if direction == "chop":
        return "better" if abs(move) <= DIRECTIONAL_CHOP_BAND else "worse"
    if direction == "up":
        if move > DIRECTIONAL_CHOP_BAND:
            return "better"
        if move < -DIRECTIONAL_CHOP_BAND:
            return "worse"
        return "tie"
    # "down" — classify() guarantees the vocabulary.
    if move < -DIRECTIONAL_CHOP_BAND:
        return "better"
    if move > DIRECTIONAL_CHOP_BAND:
        return "worse"
    return "tie"


def directional_prices(
    bars: list[OHLCBar],
    *,
    window_start: datetime,
    window_end: datetime,
    interval_minutes: int = DIRECTIONAL_GRADING_INTERVAL_MINUTES,
) -> tuple[Decimal, Decimal] | str:
    """Last-known closes at call time and horizon end, or a reason string.

    "Last known" means the close of the most recent bar that CLOSED at
    or before the moment — the price an observer had in hand, never a
    bar still forming (no lookahead into the call's own hour). A
    ``str`` return is a bars-missing reason: the caller leaves the
    suggestion in the queue, same contract as config-rec coverage.
    """
    interval = timedelta(minutes=interval_minutes)
    freshness = _MAX_EDGE_GAP_BARS * interval

    def last_close_before(moment: datetime) -> Decimal | None:
        closed = [b for b in bars if b.opened_at + interval <= moment]
        if not closed:
            return None
        latest = closed[-1]  # get_ohlc_bars returns oldest-first
        if latest.opened_at + interval < moment - freshness:
            return None  # stale: a gap sits right where the price is read
        return latest.close

    start = last_close_before(window_start)
    if start is None:
        return (
            f"no {interval_minutes}m bar closed within {_MAX_EDGE_GAP_BARS} "
            f"intervals before the call ({window_start.isoformat()})"
        )
    end = last_close_before(window_end)
    if end is None:
        return (
            f"no {interval_minutes}m bar closed within {_MAX_EDGE_GAP_BARS} "
            f"intervals before horizon end ({window_end.isoformat()})"
        )
    return start, end


def build_unscoreable(
    suggestion_id: int,
    classification: Classification,
    *,
    granularity_minutes: int | None,
    reason: str,
    scored_at: datetime | None = None,
) -> RecommendationOutcome:
    """An unscoreable row — recorded, never silently skipped (decision 5)."""
    return RecommendationOutcome(
        suggestion_id=suggestion_id,
        kind=classification.kind,
        scoreable=False,
        unscoreable_reason=reason,
        window_start=Timestamp(dt=classification.window_start),
        window_end=Timestamp(dt=classification.window_end),
        granularity_minutes=granularity_minutes,
        proposed_arm_json=None,
        inforce_arm_json=None,
        outcome=None,
        evaluator_version=EVALUATOR_VERSION,
        scored_at=Timestamp(dt=scored_at or datetime.now(UTC)),
    )


def build_directional_scored(  # pylint: disable=too-many-arguments
    suggestion_id: int,
    classification: Classification,
    *,
    direction: str,
    horizon_hours: float,
    start_price: Decimal,
    end_price: Decimal,
    outcome: Literal["better", "worse", "tie"],
    scored_at: datetime | None = None,
) -> RecommendationOutcome:
    """A graded directional row: the call + realized prices, no arms.

    Directional rows have no replay arms — ``proposed_arm_json``
    carries the grading record (the call and what the market did)
    and ``inforce_arm_json`` stays NULL. ``granularity_minutes`` is
    NULL by schema design; the bar interval the grade read from is in
    the grading record instead.
    """
    grading = {
        "direction": direction,
        "horizon_hours": horizon_hours,
        "start_price": float(start_price),
        "end_price": float(end_price),
        "move_fraction": float((end_price - start_price) / start_price),
        "chop_band_fraction": float(DIRECTIONAL_CHOP_BAND),
        "grading_interval_minutes": DIRECTIONAL_GRADING_INTERVAL_MINUTES,
    }
    return RecommendationOutcome(
        suggestion_id=suggestion_id,
        kind="directional_call",
        scoreable=True,
        unscoreable_reason=None,
        window_start=Timestamp(dt=classification.window_start),
        window_end=Timestamp(dt=classification.window_end),
        granularity_minutes=None,
        proposed_arm_json=grading,
        inforce_arm_json=None,
        outcome=outcome,
        evaluator_version=EVALUATOR_VERSION,
        scored_at=Timestamp(dt=scored_at or datetime.now(UTC)),
    )


def build_scored(  # pylint: disable=too-many-arguments
    suggestion_id: int,
    classification: Classification,
    *,
    granularity_minutes: int,
    proposed_arm: dict[str, Any],
    inforce_arm: dict[str, Any],
    outcome: Literal["better", "worse", "tie"],
    scored_at: datetime | None = None,
) -> RecommendationOutcome:
    """A scored row: both arm summaries plus the sign."""
    return RecommendationOutcome(
        suggestion_id=suggestion_id,
        kind=classification.kind,
        scoreable=True,
        unscoreable_reason=None,
        window_start=Timestamp(dt=classification.window_start),
        window_end=Timestamp(dt=classification.window_end),
        granularity_minutes=granularity_minutes,
        proposed_arm_json=proposed_arm,
        inforce_arm_json=inforce_arm,
        outcome=outcome,
        evaluator_version=EVALUATOR_VERSION,
        scored_at=Timestamp(dt=scored_at or datetime.now(UTC)),
    )

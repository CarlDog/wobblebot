"""HeuristicAdvisorAdapter — deterministic guard layer for the advisor.

A zero-cost, fully-transparent guard layer: given a metrics window it
fires one of four override guards (the clear, mechanical calls) and
defers everything else to the LLM. It is the deterministic half of the
heuristic+LLM cascade (see ``adapters/cascading_advisor.py``) and can
also run standalone (``engine: heuristic``) for a $0, offline-safe
advisor whose only opinions are the guards.

**Charter (ADR-022).** The heuristic makes only the *clear* calls — the
four guards below — and hands every other tick to the LLM as a free
judge. It deliberately does NOT tune spacing toward a volatility curve:
that ``vol→spacing`` first-order logic was retired because its ceiling
sat below the deployed grid, so it mechanically recommended TIGHTEN on
~every non-guard tick and drowned out the LLM's trackable signal. The
LLM, called on every non-guard tick, reasons about the regime without a
prescribed target — see ``config/prompts/quant.md``.

**Guards** (priority order — the first that fires wins, ``clear_match=True``):

1. **directional_runaway** (0 cycles + sharp drawdown → HOLD): price
   ran away; re-anchoring is the fix, not spacing.
2. **defensive_drawdown** (sharp drawdown, still cycling → WIDEN):
   capital preservation overrides the calm-tighten instinct. Its widen
   floor still reads the ``_ideal`` curve — the curve's one surviving use.
3. **dont_fix_working** (high win + cycles, shallow drawdown → HOLD):
   don't disrupt a configuration that's printing fills.
4. **fee_floor_calm** (near the fee floor in dead calm → HOLD): can't
   profitably tighten further; a tiny widen just churns.

When no guard fires the verdict is a non-clear HOLD (``clear_match=False``,
reason ``no_guard_fired``): the cascade reads that flag and escalates to
the LLM. The no-grid case is likewise non-clear (the LLM judges a fresh
grid's first spacing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from wobblebot.config.heuristic import HeuristicSpec
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    ConfidenceLevel,
    PerformanceSummary,
)

Direction = Literal["widen", "hold", "tighten"]


@dataclass(frozen=True)
class HeuristicVerdict:
    """Result of a single heuristic evaluation.

    ``recommendation`` is the ready-to-persist ``AdvisorRecommendation``;
    ``direction`` is the coarse call; ``clear_match`` is the cascade's
    escalation signal (``False`` → defer to the LLM); ``reason`` names
    the path that decided (a guard name or the first-order branch) for
    logging.
    """

    recommendation: AdvisorRecommendation
    direction: Direction
    clear_match: bool
    reason: str


class HeuristicAdvisorAdapter(AdvisorPort):
    """Deterministic, config-driven guard layer (``AdvisorPort``).

    Args:
        spec: Validated :class:`HeuristicSpec` (curve + thresholds +
            guard toggles). Load it with
            ``wobblebot.config.heuristic.load_heuristic_spec``.
        role: Value for ``AdvisorRecommendation.role``. Defaults to
            ``"heuristic"`` so the audit trail distinguishes a
            heuristic-decided tick from an LLM one.
    """

    def __init__(self, *, spec: HeuristicSpec, role: str = "heuristic") -> None:
        self._spec = spec
        self._role = role

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        """Return the heuristic's recommendation (the cascade calls
        :meth:`evaluate` instead to read the clear-match signal)."""
        return self.evaluate(summary).recommendation

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Heuristic output is bounds-checked by construction; the
        auto-apply gate (Stage 3.4b) enforces magnitude caps."""
        del recommendation
        return True

    # ------------------------------------------------------------------
    # Core logic (synchronous, pure — the async port just wraps it)
    # ------------------------------------------------------------------

    def evaluate(self, summary: PerformanceSummary) -> HeuristicVerdict:
        """Decide WIDEN / HOLD / TIGHTEN deterministically from metrics."""
        vol = summary.volatility
        current = summary.current_grid.spacing_percentage
        dd = summary.max_drawdown
        win = summary.win_rate
        cycles = summary.cycle_count
        guards = self._spec.guards

        # No usable current spacing — can't reason about a direction.
        # Hold, but flag non-clear so a cascade hands a fresh grid to the LLM.
        if current is None or current <= 0:
            return self._verdict(
                direction="hold",
                target=None,
                clear_match=False,
                reason="no_current_grid",
                rationale=(
                    "No current grid spacing supplied — holding; a fresh grid's "
                    "first spacing call needs the LLM advisor's judgment."
                ),
                confidence="low",
            )

        # -- Guard 4: directional runaway (price ran away, 0 round-trips) --
        g4 = guards.directional_runaway
        if g4.enabled and cycles == 0 and dd <= g4.threshold:
            return self._verdict(
                direction="hold",
                target=None,
                clear_match=True,
                reason="directional_runaway",
                rationale=(
                    f"Price ran away ({cycles} completed cycles, {dd * 100:.1f}% "
                    "drawdown) — a spacing change can't fix a directional run; "
                    "holding (re-anchoring is the fix)."
                ),
                confidence="high",
            )

        # -- Guard 3: defensive drawdown (sharp drawdown, still cycling) --
        g3 = guards.defensive_drawdown
        if g3.enabled and dd <= g3.threshold:
            # The widen floor still reads the vol curve — the curve's one
            # surviving use after the first-order logic was retired (ADR-022).
            target = max(current * g3.widen_factor, self._ideal(vol))
            return self._verdict(
                direction="widen",
                target=target,
                clear_match=True,
                reason="defensive_drawdown",
                rationale=(
                    f"Sharp drawdown ({dd * 100:.1f}%) — widening "
                    f"{current:.2f}% → {target:.2f}% for capital preservation, "
                    "overriding the calm-market tighten instinct."
                ),
                confidence="high",
            )

        # -- Guard 2: don't fix what's working --
        g2 = guards.dont_fix_working
        if (
            g2.enabled
            and win >= g2.win_rate_min
            and cycles >= g2.cycles_min
            and dd >= g2.drawdown_max
        ):
            return self._verdict(
                direction="hold",
                target=None,
                clear_match=True,
                reason="dont_fix_working",
                rationale=(
                    f"Grid is working ({win * 100:.0f}% win over {cycles} cycles, "
                    f"{dd * 100:.1f}% drawdown) — holding even though volatility "
                    "would suggest a different spacing; don't disrupt fills."
                ),
                confidence="high",
            )

        # -- Guard 1: near the fee floor in a dead-calm market --
        g1 = guards.fee_floor_calm
        if g1.enabled and current <= g1.near_floor_spacing and vol <= g1.calm_vol:
            return self._verdict(
                direction="hold",
                target=None,
                clear_match=True,
                reason="fee_floor_calm",
                rationale=(
                    f"Spacing {current:.2f}% is at the fee floor in a dead-calm "
                    f"market ({vol * 100:.2f}%/tick) — holding; can't profitably "
                    f"tighten below ~{self._spec.fee_floor:.2f}% (2× maker fee)."
                ),
                confidence="high",
            )

        # -- No guard fired: defer to the LLM free judge (ADR-022). The
        # cascade reads clear_match=False and escalates to the LLM; a
        # standalone ``engine: heuristic`` advisor simply holds.
        return self._verdict(
            direction="hold",
            target=None,
            clear_match=False,
            reason="no_guard_fired",
            rationale=(
                "No guard condition met — deferring to the LLM advisor for "
                "regime-aware judgment; the heuristic makes only the clear calls."
            ),
            confidence="low",
        )

    def _ideal(self, vol: float) -> float:
        """Piecewise-linear ``ideal(vol)``, flat-clamped at the ends and
        floored at ``fee_floor``.

        Used only by the ``defensive_drawdown`` guard to set its widen
        floor — the curve no longer drives any first-order decision
        (ADR-022).
        """
        curve = self._spec.curve  # validated: sorted by strictly increasing vol
        floor = self._spec.fee_floor
        if vol <= curve[0].vol:
            return max(curve[0].spacing, floor)
        if vol >= curve[-1].vol:
            return max(curve[-1].spacing, floor)
        for lo, hi in zip(curve, curve[1:]):
            if lo.vol <= vol <= hi.vol:
                frac = (vol - lo.vol) / (hi.vol - lo.vol)
                interp = lo.spacing + frac * (hi.spacing - lo.spacing)
                return max(interp, floor)
        # Unreachable given the bounds checks; degrades gracefully.
        return max(curve[-1].spacing, floor)

    def _verdict(  # pylint: disable=too-many-arguments
        self,
        *,
        direction: Direction,
        target: float | None,
        clear_match: bool,
        reason: str,
        rationale: str,
        confidence: ConfidenceLevel,
    ) -> HeuristicVerdict:
        """Assemble an ``AdvisorRecommendation`` + verdict metadata.

        A HOLD emits an empty ``recommendations`` dict (the prompt
        convention for "no change"); an action emits the target spacing.
        """
        recommendations: dict[str, float] = {}
        if target is not None:
            recommendations = {"spacing_percentage": round(target, 2)}
        recommendation = AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role=self._role,
            recommendations=recommendations,
            rationale=rationale,
            confidence=confidence,
        )
        return HeuristicVerdict(
            recommendation=recommendation,
            direction=direction,
            clear_match=clear_match,
            reason=reason,
        )


__all__ = ["Direction", "HeuristicAdvisorAdapter", "HeuristicVerdict"]

"""HeuristicAdvisorAdapter — Stage 8.5 deterministic ``AdvisorPort``.

A zero-cost, fully-transparent advisor: given a metrics window it
decides WIDEN / HOLD / TIGHTEN from a configured ideal-spacing-vs-
volatility curve plus four override guards — no LLM call. It is the
deterministic half of the heuristic+LLM cascade (see
``adapters/cascading_advisor.py``) and can also run standalone
(``engine: heuristic``) for a $0, offline-safe advisor.

**Two sources of truth, kept in sync deliberately.** The decision
logic this adapter executes is ALSO documented in prose in
``config/prompts/quant.md`` (the LLM advisor's system prompt), because
both halves of the cascade must reason the same way. The *numbers*
(curve + thresholds) come from a single place — the
``HeuristicSpec`` loaded from ``advisor.heuristic_file`` — so retuning
the heuristic is a config edit, not a code change. The *algorithm*
(the guards, their priority order, how they compose) lives here in
code; a new guard is a code change with a fixture-battery test by
design. When you change a threshold's *meaning* (not its value),
update ``quant.md`` to match.

**Decision logic** (validated against the 12-fixture core battery + the
8-fixture held-out battery in ``tools/probe_advisor.py``, 2026-05-29):

1. ``ideal = curve(volatility)`` (piecewise-linear, flat-clamped at the
   ends), then clamped to ``>= fee_floor``.
2. Guards, in priority order — the first that fires wins:
   - **directional_runaway** (0 cycles + sharp drawdown → HOLD): price
     ran away; re-anchoring is the fix, not spacing.
   - **defensive_drawdown** (sharp drawdown, still cycling → WIDEN):
     capital preservation overrides the calm-tighten instinct.
   - **dont_fix_working** (high win + cycles, shallow drawdown → HOLD):
     don't disrupt a configuration that's printing fills.
   - **fee_floor_calm** (near the fee floor in dead calm → HOLD): can't
     profitably tighten further; a tiny widen just churns.
3. First-order: compare ``ideal`` to current spacing. Within
   ``hold_deadband`` → HOLD; otherwise WIDEN / TIGHTEN to ``ideal``.

Each verdict carries a ``clear_match`` flag (the cascade reads it to
decide whether to escalate to the LLM): guards and clearly-inside /
clearly-outside first-order calls are clear matches; calls in the
ambiguous band around the deadband, thin metrics windows, and the
no-grid case are not.
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
    """Deterministic, config-driven ``AdvisorPort``.

    Args:
        spec: Validated :class:`HeuristicSpec` (curve + thresholds +
            guard toggles + escalation band). Load it with
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
        snapshots = summary.snapshot_count
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

        ideal = self._ideal(vol)

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
            target = max(current * g3.widen_factor, ideal)
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

        # -- First-order: ideal-vs-current with a hold deadband --
        return self._first_order(vol=vol, current=current, ideal=ideal, snapshots=snapshots)

    def _first_order(
        self, *, vol: float, current: float, ideal: float, snapshots: int
    ) -> HeuristicVerdict:
        """Direction from ``sign(ideal - current)`` outside the deadband."""
        gap = (ideal - current) / current
        abs_gap = abs(gap)
        clear_match = not self._is_ambiguous(abs_gap, snapshots)
        confidence: ConfidenceLevel = "high" if clear_match else "low"
        note = "" if clear_match else " (Borderline / thin metrics — low confidence.)"

        if abs_gap < self._spec.hold_deadband:
            return self._verdict(
                direction="hold",
                target=None,
                clear_match=clear_match,
                reason="first_order_hold",
                rationale=(
                    f"Spacing {current:.2f}% is within "
                    f"{self._spec.hold_deadband * 100:.0f}% of the ~{ideal:.2f}% ideal "
                    f"for {vol * 100:.2f}%/tick volatility — holding "
                    f"(a small re-grid isn't worth the fees).{note}"
                ),
                confidence=confidence,
            )

        direction: Direction = "widen" if gap > 0 else "tighten"
        verb = "widening" if direction == "widen" else "tightening"
        return self._verdict(
            direction=direction,
            target=ideal,
            clear_match=clear_match,
            reason="first_order_action",
            rationale=(
                f"Volatility {vol * 100:.2f}%/tick wants ~{ideal:.2f}% spacing vs "
                f"current {current:.2f}% — {verb} to {ideal:.2f}%.{note}"
            ),
            confidence=confidence,
        )

    def _is_ambiguous(self, abs_gap: float, snapshots: int) -> bool:
        """Cascade escalation signal for a first-order call.

        True when the gap straddles the hold-deadband boundary (the call
        is borderline) or the metrics window is too thin to trust the
        volatility estimate the curve keys off. Guards don't use this —
        they key off realized drawdown / cycles / win rate.
        """
        esc = self._spec.escalation
        if not esc.enabled:
            return False
        if snapshots < esc.min_snapshots:
            return True
        deadband = self._spec.hold_deadband
        return deadband * (1 - esc.margin) <= abs_gap <= deadband * (1 + esc.margin)

    def _ideal(self, vol: float) -> float:
        """Piecewise-linear ``ideal(vol)``, flat-clamped at the ends and
        floored at ``fee_floor``."""
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

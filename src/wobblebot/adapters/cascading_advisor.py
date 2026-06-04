"""CascadingAdvisorAdapter — Stage 8.5 heuristic+LLM composition.

The ``cascade`` engine: a deterministic ``HeuristicAdvisorAdapter`` runs
first; if it reports a **clear match** the (free) heuristic answer is
returned. Otherwise the call **escalates** to the wrapped LLM advisor.
If the LLM call fails (transport error) or trips the ADR-014 cost cap,
the cascade **falls back** to the heuristic's best guess — so the
advisor keeps producing answers at $0 instead of skipping the tick.
This composes the operator's three framings: deterministic-default,
LLM-pass-through, and resilient fallback.

The other two ``advisor.engine`` values don't need this wrapper:
``engine: heuristic`` builds a bare ``HeuristicAdvisorAdapter`` and
``engine: llm`` builds the bare single-LLM / MoE advisor (identical to
the pre-8.5 path). ``cli/advise._build_advisor`` does that dispatch;
this adapter exists only for the composed case.

The LLM is the primary resolver for non-guard ticks (ADR-022): the
heuristic answers only the clear guard cases for $0, and everything else
escalates. Heuristic-resolved ticks carry ``role="heuristic"`` and
escalated ticks carry the LLM's role, so the ``advisor_suggestions``
audit trail records which engine decided each tick.
"""

from __future__ import annotations

import logging

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.advisor import AdvisorPort, AdvisorRecommendation, PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError

_LOGGER = logging.getLogger("wobblebot.adapters.cascading_advisor")


class CascadingAdvisorAdapter(AdvisorPort):
    """Heuristic-first advisor that escalates non-guard ticks to an LLM.

    Args:
        heuristic: The deterministic adapter (concrete, not just
            ``AdvisorPort``, so the cascade can read its ``clear_match``
            escalation signal via ``evaluate()``).
        llm: The escalation target — any ``AdvisorPort`` (a cloud
            adapter, ``OllamaAdapter``, or ``MoEAdvisorAdapter``).
    """

    def __init__(self, *, heuristic: HeuristicAdvisorAdapter, llm: AdvisorPort) -> None:
        if heuristic is None:
            raise ValueError("CascadingAdvisorAdapter requires a heuristic advisor")
        if llm is None:
            raise ValueError("CascadingAdvisorAdapter requires an LLM advisor")
        self._heuristic = heuristic
        self._llm = llm

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        """Resolve via the heuristic; escalate to the LLM only when the
        heuristic reports a non-clear match; fall back on LLM failure."""
        verdict = self._heuristic.evaluate(summary)
        if verdict.clear_match:
            _LOGGER.info(
                "cascade: heuristic resolved (clear match)",
                extra={"reason": verdict.reason, "direction": verdict.direction},
            )
            return verdict.recommendation

        try:
            recommendation = await self._llm.get_recommendation(summary)
        except (AdvisorError, LLMCostCapExceeded) as exc:
            # Graceful degradation: a vendor outage or a tripped cost cap
            # must not strand the advisor. Fall back to the heuristic's
            # best guess (free, deterministic) and log so the operator
            # still sees the LLM was skipped.
            _LOGGER.warning(
                "cascade: LLM escalation failed; using heuristic fallback",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "reason": verdict.reason,
                    "fallback_direction": verdict.direction,
                },
            )
            return verdict.recommendation

        _LOGGER.info(
            "cascade: escalated to LLM",
            extra={"heuristic_reason": verdict.reason, "heuristic_direction": verdict.direction},
        )
        return recommendation

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Advisory-only; real bounds live in the Stage 3.4b auto-apply gate."""
        del recommendation
        return True

    async def aclose(self) -> None:
        """Release the wrapped LLM advisor's client if it owns one.

        The heuristic holds no client. ``cli/advise`` calls this at
        shutdown; an LLM advisor without ``aclose`` is skipped.
        """
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            await aclose()


__all__ = ["CascadingAdvisorAdapter"]

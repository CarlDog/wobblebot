"""MoEAdvisorAdapter — Stage 3.4a Mixture-of-Experts advisor.

Composes 2+ specialist ``AdvisorPort`` instances (today: ``OllamaAdapter``,
later: ``AnthropicAdapter``, ``OpenAIAdapter``, ``GoogleAdapter``) and
aggregates their opinions via voting / weighted_confidence / arbitrator
strategies per ADR-007.

**Fault tolerance.** Per ADR-007's "vendor outage doesn't stop the
advisor" principle: one bad expert (timeout, HTTP error, malformed
JSON) is logged with structured fields and the MoE proceeds with the
remaining experts' opinions. If ALL experts fail, the adapter raises
``AdvisorError`` — at that point there's nothing meaningful to
aggregate.

**Per-expert audit trail.** The aggregated recommendation carries
every expert's raw opinion in its ``expert_opinions`` field
(populated by this adapter, not the aggregator functions). Operator
inspection via ``tools/show_suggestions.py`` shows what each expert
said alongside the consensus.

**News-role discipline.** Per ADR-007, news opinions DO contribute
to the aggregated reasoning. The auto-apply exclusion ("news-derived
recommendations never auto-apply") lives in Stage 3.4b's gate, not
here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.aggregators import (
    aggregate_voting,
    aggregate_weighted_confidence,
)

_LOGGER = logging.getLogger("wobblebot.adapters.moe_advisor")

AggregatorStrategy = Literal["voting", "weighted_confidence"]
# arbitrator strategy lands in Slice C — it needs an LLM call so it
# can't be a pure aggregator function.

_AGGREGATORS = {
    "voting": aggregate_voting,
    "weighted_confidence": aggregate_weighted_confidence,
}


@dataclass(frozen=True)
class MoEExpertEntry:
    """One expert in the MoE lineup.

    ``advisor`` is the concrete ``AdvisorPort`` instance (today
    ``OllamaAdapter``; cloud adapters later). ``name`` is the
    operator-chosen identifier carried in the per-expert audit log.
    ``role`` is the canonical expert role used by ADR-007's
    news-never-auto-applies rule (Stage 3.4b's gate reads this).
    """

    name: str
    role: str
    advisor: AdvisorPort


class MoEAdvisorAdapter(AdvisorPort):
    """Mixture-of-Experts advisor.

    Args:
        experts: At least 2 ``MoEExpertEntry`` instances. ADR-007
            recommends ≥3 for meaningful diversity, but the adapter
            doesn't enforce a minimum — operator-side validation in
            ``AdvisorConfig`` already requires ≥3 for MoE mode.
        aggregator: Which aggregation strategy to use. ``arbitrator``
            ships in Slice C (currently unsupported here).
    """

    def __init__(
        self,
        *,
        experts: list[MoEExpertEntry],
        aggregator: AggregatorStrategy,
    ) -> None:
        if not experts:
            raise ValueError("MoE requires at least one expert")
        if aggregator not in _AGGREGATORS:
            raise ValueError(
                f"Unknown aggregator {aggregator!r}; " f"choose from {sorted(_AGGREGATORS)}"
            )
        # Operator-side AdvisorConfig validator already enforces
        # name uniqueness — re-check here so library-level callers
        # can't bypass it.
        names = [e.name for e in experts]
        if len(set(names)) != len(names):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"expert names must be unique; duplicates: {duplicates}")
        self._experts = experts
        self._aggregator_name = aggregator
        self._aggregator_fn = _AGGREGATORS[aggregator]

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        """Fan out to every expert in parallel; aggregate surviving opinions."""
        results = await asyncio.gather(
            *(self._call_expert(entry, summary) for entry in self._experts),
            return_exceptions=False,  # _call_expert catches and tags failures itself
        )
        opinions: list[AdvisorRecommendation] = [
            r for r in results if isinstance(r, AdvisorRecommendation)
        ]
        if not opinions:
            raise AdvisorError(
                f"All {len(self._experts)} MoE experts failed; no opinion to aggregate"
            )

        aggregated = self._aggregator_fn(opinions)
        # Attach per-expert opinions for the audit trail. AdvisorRecommendation
        # is frozen — re-construct with the field populated.
        return aggregated.model_copy(update={"expert_opinions": opinions})

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Stage 3.4a: passing through — like the single-LLM adapter, real
        safety-bound enforcement lives in Stage 3.4b's auto-apply gate."""
        del recommendation
        return True

    async def _call_expert(
        self,
        entry: MoEExpertEntry,
        summary: PerformanceSummary,
    ) -> AdvisorRecommendation | None:
        """Invoke one expert. Returns the opinion on success, ``None`` on
        recoverable failure (logged with structured fields). Never raises."""
        try:
            opinion = await entry.advisor.get_recommendation(summary)
        except AdvisorError as exc:
            _LOGGER.warning(
                "MoE expert failed; proceeding with remaining experts",
                extra={
                    "expert_name": entry.name,
                    "expert_role": entry.role,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return None
        # Force the expert's declared role onto the opinion — the expert
        # may emit a different role string in its JSON (some prompts
        # default to "quant" regardless), but the operator's config is
        # the source of truth for which slot this expert fills.
        return opinion.model_copy(update={"role": entry.role})

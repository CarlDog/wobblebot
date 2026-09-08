"""Explicit, bounded advisor model substitution (ADR-043).

Each candidate is an ordinary AdvisorPort with its own existing retry/cost gate.
No raw provider text is copied into logs or the persisted attempt trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    LLMAdvisorAttempt,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.aggregators import ArbitratorAdvisor
from wobblebot.services.llm_failures import FAILOVER_ERROR_KINDS, classify_error
from wobblebot.services.llm_trace import current_trace_id

_LOGGER = logging.getLogger("wobblebot.adapters.fallback_advisor")


@dataclass(frozen=True)
class AdvisorCandidate:
    """One operator-selected target; order is primary followed by alternatives."""

    provider: str
    model: str
    advisor: AdvisorPort


class FallbackAdvisorAdapter(AdvisorPort):
    """Try up to three explicit targets, stopping on success or non-outage errors."""

    def __init__(self, *, role: str, candidates: list[AdvisorCandidate]) -> None:
        if not 2 <= len(candidates) <= 3:
            raise ValueError("advisor fallback requires one primary and one or two alternatives")
        identities = [(candidate.provider, candidate.model) for candidate in candidates]
        if len(set(identities)) != len(identities):
            raise ValueError("advisor fallback targets must be distinct")
        self._role = role
        self._candidates = tuple(candidates)

    async def get_recommendation(
        self, summary: PerformanceSummary, *, extra_context: str | None = None
    ) -> AdvisorRecommendation:
        """Keep the role, summary and arbitrator context identical on each attempt.

        Only classified AdvisorError causes permit substitution. Cost-cap errors,
        cancellation and storage/programming errors propagate unchanged.
        Every new invocation begins with the primary, allowing automatic recovery.
        """
        attempts: list[LLMAdvisorAttempt] = []
        for index, candidate in enumerate(self._candidates):
            try:
                if extra_context is None:
                    result = await candidate.advisor.get_recommendation(summary)
                else:
                    result = await cast(ArbitratorAdvisor, candidate.advisor).get_recommendation(
                        summary, extra_context=extra_context
                    )
            except AdvisorError as exc:
                kind = classify_error(exc)
                attempts.append(self._attempt(candidate, kind))
                if kind not in FAILOVER_ERROR_KINDS or index == len(self._candidates) - 1:
                    raise
                next_candidate = self._candidates[index + 1]
                _LOGGER.warning(
                    "LLM fallback (role=%s, from=%s/%s, to=%s/%s, error_kind=%s, trace_id=%s)",
                    self._role,
                    candidate.provider,
                    candidate.model,
                    next_candidate.provider,
                    next_candidate.model,
                    kind,
                    current_trace_id(),
                    extra={
                        "role": self._role,
                        "from_provider": candidate.provider,
                        "from_model": candidate.model,
                        "to_provider": next_candidate.provider,
                        "to_model": next_candidate.model,
                        "error_kind": kind,
                        "trace_id": current_trace_id(),
                    },
                )
                continue
            attempts.append(self._attempt(candidate, None))
            return result.model_copy(update={"role": self._role, "llm_attempts": attempts})
        raise AssertionError("bounded fallback chain must return or raise")

    def _attempt(self, candidate: AdvisorCandidate, error: str | None) -> LLMAdvisorAttempt:
        return LLMAdvisorAttempt(
            role=self._role, provider=candidate.provider, model=candidate.model, error_kind=error
        )

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Provider adapters validate output; the existing auto-apply gate enforces bounds."""
        del recommendation
        return True

    async def aclose(self) -> None:
        """Attempt to release every candidate even when one close fails."""
        errors: list[Exception] = []
        for candidate in self._candidates:
            close = getattr(candidate.advisor, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("advisor fallback cleanup failed", errors)

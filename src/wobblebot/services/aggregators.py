"""Aggregator strategies for the Stage 3.4a MoE advisor.

Three strategies live here:

- ``aggregate_voting`` — pure function. Per-key majority across
  experts' ``recommendations`` dicts. Ties or no-majority cases omit
  the key (returns "no consensus on X" rather than fabricating one).

- ``aggregate_weighted_confidence`` — pure function. Per-key
  confidence-weighted average / mode (``high=3``, ``medium=2``,
  ``low=1``).

- ``aggregate_arbitrator`` — async, one LLM round-trip. Serializes
  the experts' opinions as context and asks a separate arbitrator
  model (typically a stronger/larger LLM) to synthesize the final
  call. Unlike the pure-function strategies it can't be invoked
  without I/O — and that's the point: ADR-007 calls out arbitrator
  as the path where the operator wants reasoned synthesis rather
  than mechanical aggregation.

**News-role inclusion:** Per ADR-007, news-derived recommendations
"contribute to the aggregated reasoning but cannot drive an
auto-applied parameter change." That auto-apply restriction lives in
Stage 3.4b's gate — here we include news opinions in the math because
they DO inform the aggregated reasoning.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    ConfidenceLevel,
    PerformanceSummary,
)


class ArbitratorAdvisor(Protocol):
    """Structural type for an advisor that accepts arbitrator context.

    OllamaAdapter satisfies this by extending ``AdvisorPort.get_recommendation``
    with an optional ``extra_context: str = ""`` keyword. Future cloud
    adapters (AnthropicAdapter, OpenAIAdapter) will add the same kwarg
    and structurally satisfy the protocol. We use a Protocol rather than
    putting ``extra_context`` on ``AdvisorPort`` itself because the kwarg
    is MoE-arbitrator-specific — the single-LLM advisor path doesn't
    need it and shouldn't be forced to thread it through.
    """

    async def get_recommendation(
        self,
        summary: PerformanceSummary,
        *,
        extra_context: str = "",
    ) -> AdvisorRecommendation: ...


_CONFIDENCE_WEIGHT: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
_AGGREGATED_ROLE = "aggregated"


def aggregate_voting(opinions: list[AdvisorRecommendation]) -> AdvisorRecommendation:
    """Per-key majority vote across experts.

    For each key seen in any opinion's ``recommendations``, the value
    with the strictly-most votes wins. Ties or below-majority cases
    omit the key from the output — the MoE returns "no consensus on
    X" rather than choosing one of two equally-popular values.

    Confidence on the aggregate:
    - All opinions agree on every key (unanimous) → ``high``.
    - At least one key has a clear winner → ``medium``.
    - No key reaches consensus → ``low``.

    Args:
        opinions: At least one expert's recommendation.

    Returns:
        Aggregated ``AdvisorRecommendation`` with ``role="aggregated"``.

    Raises:
        ValueError: If ``opinions`` is empty.
    """
    if not opinions:
        raise ValueError("aggregate_voting requires at least one opinion")

    all_keys = {k for op in opinions for k in op.recommendations}
    consensus: dict[str, Any] = {}
    per_key_counts: dict[str, Counter[Any]] = {}
    threshold = len(opinions) // 2 + 1  # strict majority

    for key in all_keys:
        # Only count opinions that actually expressed a view on this key.
        votes: Counter[Any] = Counter()
        for op in opinions:
            if key in op.recommendations:
                votes[_hashable(op.recommendations[key])] += 1
        per_key_counts[key] = votes
        if not votes:
            continue
        top_value, top_count = votes.most_common(1)[0]
        # Strict majority on the keys that were actually expressed.
        if top_count >= threshold and _no_tie(votes, top_count):
            consensus[key] = _from_hashable(top_value)

    confidence: ConfidenceLevel = _voting_confidence(opinions, consensus, all_keys)
    rationale = _voting_rationale(opinions, per_key_counts, consensus)

    return AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=_AGGREGATED_ROLE,
        recommendations=consensus,
        rationale=rationale,
        confidence=confidence,
    )


def aggregate_weighted_confidence(
    opinions: list[AdvisorRecommendation],
) -> AdvisorRecommendation:
    """Per-key confidence-weighted average / mode.

    Numeric keys produce a weighted arithmetic mean using
    ``high=3, medium=2, low=1`` weights. Integer-shaped inputs are
    rounded to the nearest int after weighting (e.g. ``levels_above``
    stays integer). Non-numeric keys fall back to a confidence-weighted
    mode — the value with the highest accumulated weight wins; ties
    omit the key (same discipline as voting).

    Aggregate confidence: weighted average of input confidence levels
    mapped back through ``high>=2.5 / medium>=1.5 / low``.

    Args:
        opinions: At least one expert's recommendation.

    Returns:
        Aggregated ``AdvisorRecommendation`` with ``role="aggregated"``.

    Raises:
        ValueError: If ``opinions`` is empty.
    """
    if not opinions:
        raise ValueError("aggregate_weighted_confidence requires at least one opinion")

    all_keys = {k for op in opinions for k in op.recommendations}
    weighted: dict[str, Any] = {}

    for key in all_keys:
        contributing = [op for op in opinions if key in op.recommendations]
        if not contributing:
            continue
        values = [op.recommendations[key] for op in contributing]
        weights = [_CONFIDENCE_WEIGHT[op.confidence] for op in contributing]
        if _all_numeric(values):
            avg = sum(v * w for v, w in zip(values, weights)) / sum(weights)
            if _all_int(values):
                weighted[key] = round(avg)
            else:
                weighted[key] = round(avg, 4)
        else:
            picked = _weighted_mode(values, weights)
            if picked is not None:
                weighted[key] = picked

    confidence = _weighted_confidence(opinions)
    rationale = _weighted_rationale(opinions, weighted)

    return AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=_AGGREGATED_ROLE,
        recommendations=weighted,
        rationale=rationale,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hashable(value: Any) -> Any:
    """Convert dicts/lists to hashable forms so Counter can count them."""
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted(value.items())))
    if isinstance(value, list):
        return ("__list__", tuple(value))
    return value


def _from_hashable(value: Any) -> Any:
    """Reverse :func:`_hashable` for output."""
    if isinstance(value, tuple) and len(value) == 2:
        tag, payload = value
        if tag == "__dict__":
            return dict(payload)
        if tag == "__list__":
            return list(payload)
    return value


def _no_tie(votes: Counter[Any], top_count: int) -> bool:
    """True iff exactly one value has ``top_count`` votes."""
    return sum(1 for c in votes.values() if c == top_count) == 1


def _all_numeric(values: list[Any]) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _all_int(values: list[Any]) -> bool:
    return all(isinstance(v, int) and not isinstance(v, bool) for v in values)


def _weighted_mode(values: list[Any], weights: list[int]) -> Any | None:
    """Return the value with the highest accumulated weight; None on tie."""
    totals: Counter[Any] = Counter()
    for v, w in zip(values, weights):
        totals[_hashable(v)] += w
    if not totals:
        return None
    top_value, top_total = totals.most_common(1)[0]
    if sum(1 for t in totals.values() if t == top_total) != 1:
        return None
    return _from_hashable(top_value)


def _voting_confidence(
    opinions: list[AdvisorRecommendation],
    consensus: dict[str, Any],
    all_keys: set[str],
) -> ConfidenceLevel:
    if not all_keys:
        return "low"
    # Unanimous on every key that anyone proposed: high.
    if _is_unanimous(opinions, all_keys):
        return "high"
    if consensus:
        return "medium"
    return "low"


def _is_unanimous(opinions: list[AdvisorRecommendation], all_keys: set[str]) -> bool:
    """True iff every expert that proposed a key agreed on its value."""
    for key in all_keys:
        seen: set[Any] = set()
        for op in opinions:
            if key in op.recommendations:
                seen.add(_hashable(op.recommendations[key]))
        if len(seen) > 1:
            return False
    return True


def _weighted_confidence(opinions: list[AdvisorRecommendation]) -> ConfidenceLevel:
    weights = [_CONFIDENCE_WEIGHT[op.confidence] for op in opinions]
    avg = sum(weights) / len(weights)
    if avg >= 2.5:
        return "high"
    if avg >= 1.5:
        return "medium"
    return "low"


def _voting_rationale(
    opinions: list[AdvisorRecommendation],
    per_key_counts: dict[str, Counter[Any]],
    consensus: dict[str, Any],
) -> str:
    n = len(opinions)
    if not per_key_counts:
        return f"Voting aggregation across {n} experts. No keys proposed."
    parts = [f"Voting aggregation across {n} experts."]
    for key, votes in sorted(per_key_counts.items()):
        if key in consensus:
            top_value, top_count = votes.most_common(1)[0]
            parts.append(f"{key}: {_from_hashable(top_value)} ({top_count}/{n})")
        else:
            distribution = ", ".join(f"{_from_hashable(v)}x{c}" for v, c in votes.most_common())
            parts.append(f"{key}: no consensus [{distribution}]")
    return " ".join(parts)


def _weighted_rationale(
    opinions: list[AdvisorRecommendation],
    weighted: dict[str, Any],
) -> str:
    n = len(opinions)
    if not weighted:
        return f"Weighted-confidence aggregation across {n} experts. No keys produced."
    confidence_dist = Counter(op.confidence for op in opinions)
    parts = [
        f"Weighted-confidence aggregation across {n} experts "
        f"(confidence mix: {dict(confidence_dist)})."
    ]
    for key, value in sorted(weighted.items()):
        parts.append(f"{key}: {value}")
    return " ".join(parts)


async def aggregate_arbitrator(
    opinions: list[AdvisorRecommendation],
    arbitrator: ArbitratorAdvisor,
    summary: PerformanceSummary,
) -> AdvisorRecommendation:
    """Have a separate arbitrator LLM synthesize the experts' opinions.

    Builds a context string from the surviving experts' opinions and
    invokes ``arbitrator.get_recommendation(summary, extra_context=...)``.
    The arbitrator sees the same ``PerformanceSummary`` the experts saw
    plus a JSON dump of their per-role opinions, and returns one final
    ``AdvisorRecommendation``. Unlike the pure-function aggregators
    this path does I/O — typically a single Ollama or cloud-LLM call.

    The returned recommendation's ``role`` is forced to ``"arbitrator"``
    regardless of what the arbitrator model self-tags. The MoE adapter
    re-stamps it again to ``"aggregated"`` when populating
    ``expert_opinions`` so the audit-trail field on the final result
    matches the other aggregators' convention.

    Args:
        opinions: Surviving expert opinions (at least one).
        arbitrator: An ``ArbitratorAdvisor`` (typically a cloud or
            heavyweight local LLM); the operator wires it via
            ``AdvisorConfig.arbitrator``.
        summary: The same performance summary the experts saw.

    Returns:
        Final synthesized recommendation with ``role="arbitrator"``.

    Raises:
        ValueError: If ``opinions`` is empty.
        AdvisorError: If the arbitrator's LLM call fails (propagated).
    """
    if not opinions:
        raise ValueError("aggregate_arbitrator requires at least one opinion")

    context = _arbitrator_context(opinions)
    result = await arbitrator.get_recommendation(summary, extra_context=context)
    # Force the role — arbitrator prompts often default to "single" or
    # whatever role string the model was trained on. The MoE adapter
    # re-stamps to "aggregated" on the final output; we tag this
    # intermediate as "arbitrator" so the audit trail keeps it
    # distinguishable from a raw single-LLM call.
    return result.model_copy(update={"role": "arbitrator"})


def _arbitrator_context(opinions: list[AdvisorRecommendation]) -> str:
    """Serialize per-expert opinions as a context block for the arbitrator."""
    serialized = [
        {
            "role": op.role,
            "confidence": op.confidence,
            "recommendations": op.recommendations,
            "rationale": op.rationale,
        }
        for op in opinions
    ]
    payload = json.dumps(serialized, indent=2, sort_keys=True)
    return (
        f"Other experts' opinions on this same metrics window "
        f"({len(opinions)} expert(s)):\n\n"
        f"{payload}\n\n"
        "Synthesize a final recommendation. You may agree with one "
        "expert, average them, or override based on the rationales."
    )

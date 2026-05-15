"""Unit tests for MoE aggregator pure functions (Stage 3.4a Slice A)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation
from wobblebot.services.aggregators import (
    aggregate_voting,
    aggregate_weighted_confidence,
)

pytestmark = pytest.mark.unit


def _opinion(
    *,
    role: str = "quant",
    recommendations: dict[str, Any] | None = None,
    confidence: str = "medium",
    rationale: str = "test",
) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id=f"rec-{role}-{confidence}",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=role,
        recommendations=recommendations or {},
        rationale=rationale,
        confidence=confidence,  # type: ignore[arg-type]
    )


class TestVotingHappyPath:
    def test_unanimous_yields_high_confidence(self) -> None:
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.2}, confidence="medium"),
            _opinion(recommendations={"spacing_percentage": 1.2}, confidence="low"),
            _opinion(recommendations={"spacing_percentage": 1.2}, confidence="high"),
        ]
        result = aggregate_voting(opinions)
        assert result.recommendations == {"spacing_percentage": 1.2}
        assert result.confidence == "high"
        assert result.role == "aggregated"

    def test_strict_majority_wins(self) -> None:
        opinions = [
            _opinion(recommendations={"levels_above": 4}),
            _opinion(recommendations={"levels_above": 4}),
            _opinion(recommendations={"levels_above": 5}),
        ]
        result = aggregate_voting(opinions)
        assert result.recommendations == {"levels_above": 4}
        assert result.confidence == "medium"

    def test_two_way_tie_omits_key(self) -> None:
        """50/50 split has no strict majority — key omitted from output."""
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.0}),
            _opinion(recommendations={"spacing_percentage": 1.2}),
        ]
        result = aggregate_voting(opinions)
        assert "spacing_percentage" not in result.recommendations
        assert result.confidence == "low"

    def test_three_way_tie_omits_key(self) -> None:
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.0}),
            _opinion(recommendations={"spacing_percentage": 1.2}),
            _opinion(recommendations={"spacing_percentage": 1.5}),
        ]
        result = aggregate_voting(opinions)
        assert result.recommendations == {}

    def test_partial_proposals_count_toward_threshold(self) -> None:
        """When only some experts express a view on a key, the threshold is
        a strict majority of ALL opinions — not just the expressing ones.
        Two votes out of three experts agreeing on a key wins because
        2 > 3/2."""
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.2}),
            _opinion(recommendations={"spacing_percentage": 1.2}),
            _opinion(recommendations={"levels_above": 4}),  # no spacing view
        ]
        result = aggregate_voting(opinions)
        assert result.recommendations.get("spacing_percentage") == 1.2

    def test_unanimous_but_partial_still_counts(self) -> None:
        """Edge case: one expert proposes a key, no one else does. Single vote
        out of three is NOT a majority — key omitted."""
        opinions = [
            _opinion(recommendations={"new_key": 42}),
            _opinion(recommendations={"spacing_percentage": 1.0}),
            _opinion(recommendations={"spacing_percentage": 1.0}),
        ]
        result = aggregate_voting(opinions)
        assert "new_key" not in result.recommendations

    def test_independent_keys_each_evaluated_separately(self) -> None:
        opinions = [
            _opinion(
                recommendations={"spacing_percentage": 1.2, "levels_above": 4},
                confidence="medium",
            ),
            _opinion(
                recommendations={"spacing_percentage": 1.2, "levels_above": 5},
                confidence="medium",
            ),
            _opinion(
                recommendations={"spacing_percentage": 1.2, "levels_above": 4},
                confidence="medium",
            ),
        ]
        result = aggregate_voting(opinions)
        assert result.recommendations == {"spacing_percentage": 1.2, "levels_above": 4}

    def test_rationale_mentions_distribution(self) -> None:
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.0}),
            _opinion(recommendations={"spacing_percentage": 1.2}),
            _opinion(recommendations={"spacing_percentage": 1.2}),
        ]
        result = aggregate_voting(opinions)
        # Rationale should reference the vote tally
        assert "spacing_percentage" in result.rationale
        assert "1.2" in result.rationale

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one opinion"):
            aggregate_voting([])

    def test_single_opinion_passes_through(self) -> None:
        opinions = [_opinion(recommendations={"spacing_percentage": 1.0}, confidence="high")]
        result = aggregate_voting(opinions)
        assert result.recommendations == {"spacing_percentage": 1.0}
        # Single-vote unanimous = high confidence
        assert result.confidence == "high"


class TestWeightedConfidenceNumeric:
    def test_simple_average(self) -> None:
        """All medium confidence → simple arithmetic mean."""
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.0}, confidence="medium"),
            _opinion(recommendations={"spacing_percentage": 1.2}, confidence="medium"),
            _opinion(recommendations={"spacing_percentage": 1.4}, confidence="medium"),
        ]
        result = aggregate_weighted_confidence(opinions)
        # (1.0 + 1.2 + 1.4) / 3 = 1.2
        assert result.recommendations["spacing_percentage"] == pytest.approx(1.2)

    def test_high_confidence_dominates(self) -> None:
        """high=3 vote outweighs two low=1 votes."""
        opinions = [
            _opinion(recommendations={"spacing_percentage": 1.2}, confidence="high"),
            _opinion(recommendations={"spacing_percentage": 0.5}, confidence="low"),
            _opinion(recommendations={"spacing_percentage": 0.5}, confidence="low"),
        ]
        result = aggregate_weighted_confidence(opinions)
        # (1.2*3 + 0.5*1 + 0.5*1) / (3+1+1) = 4.6/5 = 0.92
        assert result.recommendations["spacing_percentage"] == pytest.approx(0.92)

    def test_integer_keys_rounded(self) -> None:
        """Levels_above stays integer after weighted average."""
        opinions = [
            _opinion(recommendations={"levels_above": 3}, confidence="medium"),
            _opinion(recommendations={"levels_above": 4}, confidence="medium"),
            _opinion(recommendations={"levels_above": 5}, confidence="medium"),
        ]
        result = aggregate_weighted_confidence(opinions)
        # Avg = 4.0 → 4 (int)
        assert result.recommendations["levels_above"] == 4
        assert isinstance(result.recommendations["levels_above"], int)

    def test_mixed_int_proposals_get_rounded(self) -> None:
        """Avg 3.67 → 4 (banker's rounding handles .5 cases predictably)."""
        opinions = [
            _opinion(recommendations={"levels_above": 3}, confidence="medium"),
            _opinion(recommendations={"levels_above": 4}, confidence="medium"),
            _opinion(recommendations={"levels_above": 4}, confidence="medium"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert result.recommendations["levels_above"] == 4
        assert isinstance(result.recommendations["levels_above"], int)


class TestWeightedConfidenceNonNumeric:
    def test_string_falls_back_to_weighted_mode(self) -> None:
        """Non-numeric values can't average — use weighted mode."""
        opinions = [
            _opinion(recommendations={"strategy": "buy_low"}, confidence="high"),
            _opinion(recommendations={"strategy": "buy_low"}, confidence="medium"),
            _opinion(recommendations={"strategy": "sell_high"}, confidence="low"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert result.recommendations["strategy"] == "buy_low"

    def test_string_tied_weights_omits_key(self) -> None:
        opinions = [
            _opinion(recommendations={"strategy": "a"}, confidence="high"),
            _opinion(recommendations={"strategy": "b"}, confidence="high"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert "strategy" not in result.recommendations


class TestWeightedConfidenceLevel:
    def test_all_high_yields_high(self) -> None:
        opinions = [
            _opinion(recommendations={"x": 1}, confidence="high"),
            _opinion(recommendations={"x": 1}, confidence="high"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert result.confidence == "high"

    def test_all_low_yields_low(self) -> None:
        opinions = [
            _opinion(recommendations={"x": 1}, confidence="low"),
            _opinion(recommendations={"x": 1}, confidence="low"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert result.confidence == "low"

    def test_mixed_yields_medium(self) -> None:
        opinions = [
            _opinion(recommendations={"x": 1}, confidence="high"),
            _opinion(recommendations={"x": 1}, confidence="low"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert result.confidence == "medium"


class TestWeightedConfidenceEdgeCases:
    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one opinion"):
            aggregate_weighted_confidence([])

    def test_single_opinion_passes_through(self) -> None:
        opinions = [_opinion(recommendations={"x": 1.5}, confidence="high")]
        result = aggregate_weighted_confidence(opinions)
        assert result.recommendations == {"x": 1.5}
        assert result.confidence == "high"

    def test_rationale_includes_confidence_mix(self) -> None:
        opinions = [
            _opinion(recommendations={"x": 1.0}, confidence="high"),
            _opinion(recommendations={"x": 2.0}, confidence="medium"),
        ]
        result = aggregate_weighted_confidence(opinions)
        assert "confidence mix" in result.rationale

    def test_role_is_aggregated(self) -> None:
        opinions = [
            _opinion(role="quant", recommendations={"x": 1}, confidence="medium"),
            _opinion(role="risk", recommendations={"x": 1}, confidence="medium"),
        ]
        for func in (aggregate_voting, aggregate_weighted_confidence):
            result = func(opinions)
            assert result.role == "aggregated"

    def test_recommendation_id_is_unique(self) -> None:
        """Two aggregations of the same input get distinct IDs (fresh UUIDs)."""
        opinions = [_opinion(recommendations={"x": 1.0}, confidence="medium")]
        a = aggregate_voting(opinions)
        b = aggregate_voting(opinions)
        assert a.recommendation_id != b.recommendation_id


class TestNewsRoleParticipation:
    """Per ADR-007, news-role opinions DO contribute to the aggregated
    reasoning — only auto-apply (Stage 3.4b) excludes them."""

    def test_news_opinion_counts_in_voting(self) -> None:
        opinions = [
            _opinion(role="quant", recommendations={"spacing_percentage": 1.0}),
            _opinion(role="risk", recommendations={"spacing_percentage": 1.0}),
            _opinion(role="news", recommendations={"spacing_percentage": 1.0}),
        ]
        result = aggregate_voting(opinions)
        # All three agree; unanimous → high
        assert result.confidence == "high"
        assert result.recommendations == {"spacing_percentage": 1.0}

    def test_news_opinion_weighted_into_average(self) -> None:
        opinions = [
            _opinion(role="quant", recommendations={"spacing_percentage": 1.0}, confidence="high"),
            _opinion(role="news", recommendations={"spacing_percentage": 2.0}, confidence="high"),
        ]
        result = aggregate_weighted_confidence(opinions)
        # Both high → (1.0*3 + 2.0*3) / 6 = 1.5
        assert result.recommendations["spacing_percentage"] == pytest.approx(1.5)

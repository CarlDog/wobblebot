"""Unit tests for the Stage 3.4b auto-apply gate (services/auto_apply.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from wobblebot.config.advisor import AutoApplyConfig
from wobblebot.config.grid import GridLevels
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.services.auto_apply import (
    AutoApplyResult,
    evaluate_auto_apply,
)

pytestmark = pytest.mark.unit


def _grid(
    *,
    spacing: str = "1.0",
    levels_above: int = 3,
    levels_below: int = 3,
    order_size: str = "10",
) -> GridLevels:
    return GridLevels(
        spacing_percentage=Decimal(spacing),
        levels_above=levels_above,
        levels_below=levels_below,
        order_size_usd=Decimal(order_size),
    )


def _suggestion(
    *,
    role: str = "aggregated",
    confidence: str = "medium",
    recommendations: dict[str, Any] | None = None,
) -> AdvisorSuggestion:
    rec = AdvisorRecommendation(
        recommendation_id="rec-test",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=role,
        recommendations=recommendations or {},
        rationale="test",
        confidence=confidence,  # type: ignore[arg-type]
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary={},
        model_name="phi4:14b",
    )


def _auto_apply(
    *,
    enabled: bool = True,
    max_spacing_pct: str = "20",
    max_order_size_pct: str = "15",
) -> AutoApplyConfig:
    return AutoApplyConfig(
        enabled=enabled,
        max_spacing_change_percentage=Decimal(max_spacing_pct),
        max_order_size_change_percentage=Decimal(max_order_size_pct),
    )


class TestEnabledFlag:
    def test_disabled_blanket_rejects(self) -> None:
        """``enabled=False`` rejects every key with a clear reason —
        even a key that would otherwise pass every other check."""
        suggestion = _suggestion(
            recommendations={"spacing_percentage": 1.05, "order_size_usd": 10.5},
        )
        result = evaluate_auto_apply(
            suggestion,
            _grid(),
            _auto_apply(enabled=False),
            symbol="BTC",
        )
        assert result.enabled is False
        assert result.applied_keys == []
        assert {r.key for r in result.rejected_keys} == {
            "spacing_percentage",
            "order_size_usd",
        }
        for rejected in result.rejected_keys:
            assert "auto-apply disabled" in rejected.reason

    def test_disabled_proposed_grid_unchanged(self) -> None:
        current = _grid(spacing="1.0", order_size="10")
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.1})
        result = evaluate_auto_apply(
            suggestion,
            current,
            _auto_apply(enabled=False),
            symbol="BTC",
        )
        assert result.proposed_grid == current


class TestRoleEligibility:
    def test_news_role_blanket_rejects(self) -> None:
        """Per ADR-007 a single-LLM news-role suggestion never auto-applies."""
        suggestion = _suggestion(
            role="news",
            recommendations={"spacing_percentage": 1.1},
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.role_eligible is False
        assert result.applied_keys == []
        reason = result.rejected_keys[0].reason
        assert "role='news'" in reason
        assert "ADR-007" in reason

    def test_aggregated_role_with_news_in_opinions_still_applies(self) -> None:
        """An MoE-aggregated recommendation that included a news expert
        in expert_opinions still applies for whitelisted keys — the
        ``aggregated`` role IS the metrics-driven synthesis. The
        news-blocking rule is about role, not contributing experts."""
        # We construct the suggestion directly; the expert_opinions field
        # carries the news opinion but the top-level role is "aggregated".
        news_op = AdvisorRecommendation(
            recommendation_id="op-news",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="news",
            recommendations={"spacing_percentage": 1.5},
            rationale="news",
            confidence="high",
        )
        rec = AdvisorRecommendation(
            recommendation_id="rec-aggregated",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="aggregated",
            recommendations={"spacing_percentage": 1.1},
            rationale="consensus",
            confidence="medium",
            expert_opinions=[news_op],
        )
        suggestion = AdvisorSuggestion(
            recommendation=rec,
            created_at=Timestamp(dt=datetime.now(UTC)),
            input_summary={},
            model_name="moe[voting:...]",
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.role_eligible is True
        assert len(result.applied_keys) == 1
        assert result.applied_keys[0].key == "spacing_percentage"

    def test_single_role_applies(self) -> None:
        """Stage 3.2 single-LLM suggestions use role='single' — they're
        metrics-driven and eligible."""
        suggestion = _suggestion(
            role="single",
            recommendations={"spacing_percentage": 1.1},
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.role_eligible is True
        assert len(result.applied_keys) == 1

    def test_quant_role_applies(self) -> None:
        """role='quant' (per-expert opinion that found its way into the
        top-level recommendation somehow) is still metrics-driven."""
        suggestion = _suggestion(
            role="quant",
            recommendations={"spacing_percentage": 1.1},
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.role_eligible is True


class TestKeyWhitelist:
    def test_level_keys_rejected_with_dedicated_reason(self) -> None:
        suggestion = _suggestion(
            recommendations={"levels_above": 4, "levels_below": 4},
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        rejected_by_key = {r.key: r for r in result.rejected_keys}
        for key in ("levels_above", "levels_below"):
            assert key in rejected_by_key
            assert "no magnitude cap configured" in rejected_by_key[key].reason

    def test_unknown_key_rejected(self) -> None:
        suggestion = _suggestion(recommendations={"orange_juice_per_tick": 42.0})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert len(result.rejected_keys) == 1
        assert result.rejected_keys[0].key == "orange_juice_per_tick"
        assert "not whitelisted" in result.rejected_keys[0].reason

    def test_mixed_keys_partial_apply(self) -> None:
        """One whitelisted key passes, one level key rejected, one
        unknown rejected. Operator should see all three outcomes."""
        suggestion = _suggestion(
            recommendations={
                "spacing_percentage": 1.05,
                "levels_above": 5,
                "mystery_field": "x",
            },
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert {a.key for a in result.applied_keys} == {"spacing_percentage"}
        assert {r.key for r in result.rejected_keys} == {"levels_above", "mystery_field"}


class TestMagnitudeCaps:
    def test_within_cap_passes(self) -> None:
        """20% cap on spacing; 1.0 → 1.15 is +15%, within the cap."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.15})
        result = evaluate_auto_apply(
            suggestion,
            _grid(spacing="1.0"),
            _auto_apply(max_spacing_pct="20"),
            symbol="BTC",
        )
        applied = result.applied_keys[0]
        assert applied.key == "spacing_percentage"
        assert applied.before == 1.0
        assert applied.after == 1.15
        assert applied.delta_pct == pytest.approx(15.0)

    def test_exactly_at_cap_passes(self) -> None:
        """Inclusive cap: a delta exactly equal to the configured max
        clears (≤, not <)."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.2})
        result = evaluate_auto_apply(
            suggestion,
            _grid(spacing="1.0"),
            _auto_apply(max_spacing_pct="20"),
            symbol="BTC",
        )
        assert len(result.applied_keys) == 1
        assert result.applied_keys[0].delta_pct == pytest.approx(20.0)

    def test_above_cap_rejected_with_delta_in_reason(self) -> None:
        """1.0 → 1.5 is +50%, well above 20% cap."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.5})
        result = evaluate_auto_apply(
            suggestion,
            _grid(spacing="1.0"),
            _auto_apply(max_spacing_pct="20"),
            symbol="BTC",
        )
        assert result.applied_keys == []
        reason = result.rejected_keys[0].reason
        assert "+50.00%" in reason
        assert "20" in reason

    def test_below_zero_rejected_even_within_cap(self) -> None:
        """A 50% reduction is well under the 20% cap, but the proposed
        value 0.5 is +50% magnitude below 1.0 — the cap is on the
        ``abs(delta)``, so this still fails."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 0.5})
        result = evaluate_auto_apply(
            suggestion,
            _grid(spacing="1.0"),
            _auto_apply(max_spacing_pct="20"),
            symbol="BTC",
        )
        assert result.applied_keys == []
        assert "-50.00%" in result.rejected_keys[0].reason

    def test_within_cap_decrease_passes(self) -> None:
        """1.0 → 0.85 is -15%, within 20% cap."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 0.85})
        result = evaluate_auto_apply(
            suggestion,
            _grid(spacing="1.0"),
            _auto_apply(max_spacing_pct="20"),
            symbol="BTC",
        )
        assert len(result.applied_keys) == 1
        assert result.applied_keys[0].delta_pct == pytest.approx(-15.0)

    def test_order_size_cap_independent_of_spacing_cap(self) -> None:
        """Order-size has its own 15% cap; verify it's wired correctly."""
        # 10 -> 11.5 is +15% exactly — passes inclusive cap.
        passing = _suggestion(recommendations={"order_size_usd": 11.5})
        result = evaluate_auto_apply(
            passing,
            _grid(order_size="10"),
            _auto_apply(max_order_size_pct="15"),
            symbol="BTC",
        )
        assert len(result.applied_keys) == 1

        # 10 -> 12 is +20% — fails 15% cap.
        failing = _suggestion(recommendations={"order_size_usd": 12.0})
        result2 = evaluate_auto_apply(
            failing,
            _grid(order_size="10"),
            _auto_apply(max_order_size_pct="15"),
            symbol="BTC",
        )
        assert result2.applied_keys == []
        assert "+20.00%" in result2.rejected_keys[0].reason


class TestNumericCoercion:
    def test_int_proposal_coerced(self) -> None:
        """LLM may emit `8` instead of `8.0` — must still parse."""
        suggestion = _suggestion(recommendations={"order_size_usd": 9})
        result = evaluate_auto_apply(
            suggestion,
            _grid(order_size="10"),
            _auto_apply(max_order_size_pct="15"),
            symbol="BTC",
        )
        assert len(result.applied_keys) == 1
        assert result.applied_keys[0].after == 9.0

    def test_string_numeric_coerced(self) -> None:
        """Some LLMs occasionally emit numerics-as-strings."""
        suggestion = _suggestion(recommendations={"spacing_percentage": "1.05"})
        result = evaluate_auto_apply(
            suggestion,
            _grid(),
            _auto_apply(),
            symbol="BTC",
        )
        assert len(result.applied_keys) == 1

    def test_non_numeric_string_rejected(self) -> None:
        suggestion = _suggestion(recommendations={"spacing_percentage": "tighten"})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.applied_keys == []
        assert "not numeric" in result.rejected_keys[0].reason

    def test_zero_proposal_rejected(self) -> None:
        """A zero spacing or order_size would break the engine — refuse
        even if within the percent cap arithmetic."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 0})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.applied_keys == []
        assert "must be > 0" in result.rejected_keys[0].reason

    def test_negative_proposal_rejected(self) -> None:
        suggestion = _suggestion(recommendations={"order_size_usd": -5})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.applied_keys == []
        assert "must be > 0" in result.rejected_keys[0].reason

    def test_bool_proposal_rejected(self) -> None:
        """Python bools are int subtypes — guard against ``True``
        sneaking through as 1."""
        suggestion = _suggestion(recommendations={"spacing_percentage": True})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.applied_keys == []


class TestProposedGrid:
    def test_unchanged_when_nothing_applied(self) -> None:
        current = _grid()
        suggestion = _suggestion(recommendations={})
        result = evaluate_auto_apply(suggestion, current, _auto_apply(), symbol="BTC")
        assert result.proposed_grid == current

    def test_merges_only_applied_keys(self) -> None:
        """Rejected keys must NOT appear in the proposed grid."""
        current = _grid(spacing="1.0", order_size="10")
        suggestion = _suggestion(
            recommendations={
                "spacing_percentage": 1.1,  # passes
                "order_size_usd": 20,  # +100%, fails 15% cap
            },
        )
        result = evaluate_auto_apply(suggestion, current, _auto_apply(), symbol="BTC")
        assert result.proposed_grid.spacing_percentage == Decimal("1.1")
        # order_size should be the current value, not the proposed.
        assert result.proposed_grid.order_size_usd == current.order_size_usd

    def test_preserves_unchanged_level_fields(self) -> None:
        """Level fields aren't whitelisted — proposed_grid keeps them
        equal to current."""
        current = _grid(levels_above=3, levels_below=3)
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.05})
        result = evaluate_auto_apply(suggestion, current, _auto_apply(), symbol="BTC")
        assert result.proposed_grid.levels_above == 3
        assert result.proposed_grid.levels_below == 3


class TestIsCleanApply:
    def test_all_applied_returns_true(self) -> None:
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.05})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.is_clean_apply() is True

    def test_partial_apply_returns_false(self) -> None:
        """Mixed applied + rejected means the operator should review."""
        suggestion = _suggestion(
            recommendations={"spacing_percentage": 1.05, "levels_above": 4},
        )
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.is_clean_apply() is False

    def test_nothing_applied_returns_false(self) -> None:
        suggestion = _suggestion(recommendations={"levels_above": 4})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert result.is_clean_apply() is False


class TestSymbolCarriedThrough:
    def test_symbol_in_result(self) -> None:
        """The operator's audit log keys by symbol — carry it through
        from the caller to the result."""
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.1})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="ETH")
        assert result.symbol == "ETH"


class TestResultIsImmutable:
    def test_frozen_pydantic_model(self) -> None:
        """AutoApplyResult is a domain-shaped value; downstream code
        shouldn't be able to reassign top-level fields after the gate
        produces it. (Pydantic ``frozen=True`` prevents field
        reassignment but not contained-list mutation — that's fine for
        our purposes because the result is constructed once and consumed
        read-only.)"""
        suggestion = _suggestion(recommendations={"spacing_percentage": 1.05})
        result = evaluate_auto_apply(suggestion, _grid(), _auto_apply(), symbol="BTC")
        assert isinstance(result, AutoApplyResult)
        with pytest.raises((TypeError, ValueError)):
            result.symbol = "ETH"  # type: ignore[misc]

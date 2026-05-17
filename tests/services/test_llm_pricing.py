"""Tests for ``services/llm_pricing.py`` (Stage 6.1.B, ADR-014)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wobblebot.services.llm_pricing import (
    LLMPricePoint,
    PricingLookupError,
    all_price_points,
    cost_for,
    get_price_point,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- #
# Table completeness                                                    #
# --------------------------------------------------------------------- #


class TestTableCompleteness:
    """Every model Stages 6.2-6.4 will reference must be in the table.

    If any of these lookups starts failing, either: (a) Stage 6.2-6.4
    is configuring an unmodeled provider+model and needs to add it, or
    (b) the pricing table dropped an entry. Either way it's a CI
    failure, not a runtime surprise.
    """

    @pytest.mark.parametrize(
        "provider,model",
        [
            ("anthropic", "claude-sonnet-4-6"),
            ("anthropic", "claude-opus-4-7"),
            ("openai", "gpt-4o"),
            ("openai", "gpt-4o-mini"),
            ("openai", "o1"),
            ("openai", "o3-mini"),
            ("google", "gemini-2.5-pro"),
            ("google", "gemini-2.5-flash"),
        ],
    )
    def test_in_scope_model_priced(self, provider: str, model: str) -> None:
        point = get_price_point(provider, model)  # type: ignore[arg-type]
        assert point.provider == provider
        assert point.model == model
        assert point.input_per_million_usd > 0
        assert point.output_per_million_usd > 0

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(PricingLookupError, match="No pricing entry"):
            get_price_point("anthropic", "claude-mystery")  # type: ignore[arg-type]

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(PricingLookupError):
            get_price_point("cohere", "command-r")  # type: ignore[arg-type]

    def test_all_price_points_returns_full_table(self) -> None:
        points = all_price_points()
        # Every entry in the table; sanity-check there are at least the
        # 8 in-scope models above. (Test stays valid as new entries land.)
        assert len(points) >= 8
        assert all(isinstance(p, LLMPricePoint) for p in points)


# --------------------------------------------------------------------- #
# cost_for                                                              #
# --------------------------------------------------------------------- #


class TestCostFor:
    def test_input_only_call(self) -> None:
        # gpt-4o-mini: $0.15 / 1M input
        # 1000 tokens → 1000 * 0.15 / 1,000,000 = $0.00015
        cost = cost_for("openai", "gpt-4o-mini", tokens_in=1000, tokens_out=0)
        assert cost == Decimal("0.000150")

    def test_output_only_call(self) -> None:
        # gpt-4o-mini: $0.60 / 1M output
        # 1000 tokens → 1000 * 0.60 / 1,000,000 = $0.0006
        cost = cost_for("openai", "gpt-4o-mini", tokens_in=0, tokens_out=1000)
        assert cost == Decimal("0.000600")

    def test_combined_input_output(self) -> None:
        # claude-sonnet-4-6: $3/1M in, $15/1M out
        # 10k in → 10000 * 3 / 1M = $0.03
        # 5k out → 5000 * 15 / 1M = $0.075
        # total = $0.105
        cost = cost_for("anthropic", "claude-sonnet-4-6", tokens_in=10_000, tokens_out=5_000)
        assert cost == Decimal("0.105000")

    def test_reasoning_falls_back_to_output_rate_when_no_override(self) -> None:
        # o1: $15 in, $60 out, reasoning falls back to $60 (no override).
        # 100 in → $0.0015
        # 200 out → $0.012
        # 1000 reasoning at output rate → $0.060
        # total = $0.0735
        cost = cost_for("openai", "o1", tokens_in=100, tokens_out=200, tokens_reasoning=1000)
        assert cost == Decimal("0.073500")

    def test_reasoning_uses_explicit_override(self) -> None:
        # gemini-2.5-flash: $0.30 in, $2.50 out, $3.50 reasoning (override).
        # 1000 in → 1000 * 0.30 / 1M = $0.0003
        # 1000 out → 1000 * 2.50 / 1M = $0.0025
        # 1000 reasoning at $3.50 / 1M = $0.0035
        # total = $0.0063
        cost = cost_for(
            "google",
            "gemini-2.5-flash",
            tokens_in=1000,
            tokens_out=1000,
            tokens_reasoning=1000,
        )
        assert cost == Decimal("0.006300")

    def test_zero_tokens_is_zero_cost(self) -> None:
        cost = cost_for("anthropic", "claude-sonnet-4-6", 0, 0)
        assert cost == Decimal("0.000000")

    def test_unknown_model_raises_not_silent_zero(self) -> None:
        with pytest.raises(PricingLookupError):
            cost_for("anthropic", "claude-mystery", 100, 100)

    def test_negative_tokens_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            cost_for("openai", "gpt-4o", tokens_in=-1, tokens_out=100)
        with pytest.raises(ValueError, match="non-negative"):
            cost_for("openai", "gpt-4o", tokens_in=100, tokens_out=-1)
        with pytest.raises(ValueError, match="non-negative"):
            cost_for("openai", "gpt-4o", tokens_in=100, tokens_out=100, tokens_reasoning=-1)

    def test_six_decimal_precision_preserved(self) -> None:
        # 1 token of gemini-flash input: 1 * 0.30 / 1M = 0.0000003 → quantizes to 0.000000
        # 4 tokens: 4 * 0.30 / 1M = 0.0000012 → quantizes to 0.000001
        cost_4 = cost_for("google", "gemini-2.5-flash", tokens_in=4, tokens_out=0)
        assert cost_4 == Decimal("0.000001")

    def test_default_reasoning_zero_does_not_affect_cost(self) -> None:
        without = cost_for("anthropic", "claude-opus-4-7", 100, 100)
        with_zero = cost_for("anthropic", "claude-opus-4-7", 100, 100, tokens_reasoning=0)
        assert without == with_zero


# --------------------------------------------------------------------- #
# LLMPricePoint validation                                              #
# --------------------------------------------------------------------- #


class TestPricePointValidation:
    def test_frozen(self) -> None:
        point = get_price_point("anthropic", "claude-sonnet-4-6")
        with pytest.raises(Exception):
            point.input_per_million_usd = Decimal("0")  # type: ignore[misc]

    def test_negative_input_rate_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMPricePoint(
                provider="anthropic",
                model="x",
                input_per_million_usd=Decimal("-1"),
                output_per_million_usd=Decimal("1"),
                verified_date=date(2026, 1, 1),
            )

    def test_negative_reasoning_rate_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMPricePoint(
                provider="anthropic",
                model="x",
                input_per_million_usd=Decimal("1"),
                output_per_million_usd=Decimal("1"),
                reasoning_per_million_usd=Decimal("-1"),
                verified_date=date(2026, 1, 1),
            )

    def test_empty_model_rejected(self) -> None:
        with pytest.raises(Exception):
            LLMPricePoint(
                provider="anthropic",
                model="",
                input_per_million_usd=Decimal("1"),
                output_per_million_usd=Decimal("1"),
                verified_date=date(2026, 1, 1),
            )

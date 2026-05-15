"""Tests for the AdvisorConfig schema and its invariants (audit slice 2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from wobblebot.config.advisor import (
    AdvisorConfig,
    ArbitratorConfig,
    AutoApplyConfig,
    ExpertConfig,
    InferenceParams,
)

pytestmark = pytest.mark.unit


def _expert(name: str, role: str = "quant") -> ExpertConfig:
    return ExpertConfig(
        name=name,
        provider="ollama",
        model="deepseek-r1:7b",
        role=role,  # type: ignore[arg-type]
        prompt_file=f"config/prompts/{name}.md",
    )


def _arbitrator() -> ArbitratorConfig:
    return ArbitratorConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_file="config/prompts/arbitrator.md",
    )


# ---------------------------------------------------------------------------
# Single-LLM mode
# ---------------------------------------------------------------------------


class TestSingleMode:
    def test_minimal_single(self) -> None:
        cfg = AdvisorConfig(type="single")
        assert cfg.type == "single"
        assert cfg.experts == []

    def test_single_with_experts_rejected(self) -> None:
        """Loaded experts in single mode is operator confusion."""
        with pytest.raises(ValidationError, match="must not have experts"):
            AdvisorConfig(type="single", experts=[_expert("quant")])


# ---------------------------------------------------------------------------
# MoE mode
# ---------------------------------------------------------------------------


class TestMoEMode:
    def test_minimum_three_experts(self) -> None:
        cfg = AdvisorConfig(
            type="moe",
            experts=[_expert("a"), _expert("b"), _expert("c")],
        )
        assert len(cfg.experts) == 3

    def test_two_experts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 3 experts"):
            AdvisorConfig(
                type="moe",
                experts=[_expert("a"), _expert("b")],
            )

    def test_zero_experts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 3 experts"):
            AdvisorConfig(type="moe", experts=[])

    def test_five_experts_accepted(self) -> None:
        """No upper limit on expert count."""
        cfg = AdvisorConfig(
            type="moe",
            experts=[_expert(n) for n in ["a", "b", "c", "d", "e"]],
        )
        assert len(cfg.experts) == 5

    def test_duplicate_expert_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicates"):
            AdvisorConfig(
                type="moe",
                experts=[_expert("quant"), _expert("quant"), _expert("c")],
            )


# ---------------------------------------------------------------------------
# Aggregator + arbitrator coupling
# ---------------------------------------------------------------------------


class TestAggregatorArbitratorCoupling:
    def test_arbitrator_aggregator_requires_arbitrator_block(self) -> None:
        with pytest.raises(ValidationError, match="requires the `arbitrator:`"):
            AdvisorConfig(
                type="moe",
                aggregator="arbitrator",
                experts=[_expert("a"), _expert("b"), _expert("c")],
            )

    def test_arbitrator_with_block_accepted(self) -> None:
        cfg = AdvisorConfig(
            type="moe",
            aggregator="arbitrator",
            arbitrator=_arbitrator(),
            experts=[_expert("a"), _expert("b"), _expert("c")],
        )
        assert cfg.arbitrator is not None

    def test_voting_does_not_require_arbitrator(self) -> None:
        cfg = AdvisorConfig(
            type="moe",
            aggregator="voting",
            experts=[_expert("a"), _expert("b"), _expert("c")],
        )
        assert cfg.arbitrator is None

    def test_weighted_confidence_does_not_require_arbitrator(self) -> None:
        cfg = AdvisorConfig(
            type="moe",
            aggregator="weighted_confidence",
            experts=[_expert("a"), _expert("b"), _expert("c")],
        )
        assert cfg.arbitrator is None


# ---------------------------------------------------------------------------
# ExpertConfig + provider/role enums
# ---------------------------------------------------------------------------


class TestExpertConfig:
    def test_all_providers_accepted(self) -> None:
        for provider in ("ollama", "anthropic", "openai", "google"):
            cfg = ExpertConfig(
                name="x",
                provider=provider,  # type: ignore[arg-type]
                model="m",
                role="quant",
                prompt_file="x.md",
            )
            assert cfg.provider == provider

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExpertConfig(
                name="x",
                provider="cohere",  # type: ignore[arg-type]
                model="m",
                role="quant",
                prompt_file="x.md",
            )

    def test_all_roles_accepted(self) -> None:
        for role in ("quant", "risk", "news", "custom"):
            cfg = ExpertConfig(
                name=role,
                provider="ollama",
                model="m",
                role=role,  # type: ignore[arg-type]
                prompt_file="x.md",
            )
            assert cfg.role == role

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExpertConfig(name="", provider="ollama", model="m", role="quant", prompt_file="x.md")


# ---------------------------------------------------------------------------
# InferenceParams + AutoApplyConfig defaults
# ---------------------------------------------------------------------------


class TestSubConfigDefaults:
    def test_inference_params_defaults(self) -> None:
        p = InferenceParams()
        assert p.temperature == Decimal("0.5")
        assert p.max_tokens == 512

    def test_inference_params_temperature_clamped(self) -> None:
        with pytest.raises(ValidationError, match="temperature"):
            InferenceParams(temperature=Decimal("2.5"))

    def test_auto_apply_off_by_default(self) -> None:
        p = AutoApplyConfig()
        assert p.enabled is False
        assert p.max_spacing_change_percentage == Decimal("20")

"""Pydantic schemas for the Phase 3 strategy advisor.

Encodes the architecture decided in ADR-007 (MoE + news ingestion +
prompt files) and the YAML schema decided in ADR-009.

Key invariants enforced here:
- ``type=moe`` requires ``len(experts) >= 3`` (operator preference;
  no upper limit)
- ``aggregator=arbitrator`` requires the ``arbitrator:`` block be set
- Provider is constrained to known LLM hosts; new providers added
  here when their adapters land
- Prompt file paths are stored as strings; existence and parse-ability
  are checked at advisor-construction time, not config-load time
  (config validation should not do filesystem I/O)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Provider tag — extend when new adapters land. The MoE adapter
# dispatches on this string at construction time.
LLMProvider = Literal["ollama", "anthropic", "openai", "google"]

# Specialty role — informational only; the advisor uses this to
# label expert opinions in logs and (later) in the aggregator's
# prompt context. "custom" is a deliberate escape hatch for operators
# who want experts that don't fit the canonical roles.
ExpertRole = Literal["quant", "risk", "news", "custom"]

# Aggregator strategy — see ADR-007 for the rationale of each.
AggregatorStrategy = Literal["voting", "weighted_confidence", "arbitrator"]

AdvisorType = Literal["single", "moe"]


class InferenceParams(BaseModel):
    """Per-expert / per-arbitrator LLM inference knobs.

    Defaults are conservative; experts override per-role (the news
    expert might want higher temperature than the quant expert).
    """

    temperature: Decimal = Field(default=Decimal("0.5"), ge=Decimal("0"), le=Decimal("2"))
    max_tokens: int = Field(default=512, gt=0)

    class Config:
        frozen = True


class ExpertConfig(BaseModel):
    """One expert in the MoE.

    ``name`` is operator-chosen and used as the log key. ``role``
    informs the system prompt's framing but is not a runtime gate —
    the expert can output any field its prompt asks for.
    """

    name: str = Field(min_length=1)
    provider: LLMProvider
    model: str = Field(min_length=1)
    role: ExpertRole
    prompt_file: str = Field(min_length=1)
    inference_params: InferenceParams = Field(default_factory=InferenceParams)

    class Config:
        frozen = True


class ArbitratorConfig(BaseModel):
    """The optional fourth model that reads the experts' opinions and
    picks a final recommendation. Required when ``aggregator=arbitrator``.

    Best practice is a more capable / more deterministic model than
    the experts themselves — the arbitrator's job is to weigh
    arguments, not to generate novel ones.
    """

    provider: LLMProvider
    model: str = Field(min_length=1)
    prompt_file: str = Field(min_length=1)
    inference_params: InferenceParams = Field(default_factory=InferenceParams)

    class Config:
        frozen = True


class AutoApplyConfig(BaseModel):
    """Bounded auto-tuning gate. Off by default.

    Per ADR-007, news-derived recommendations NEVER auto-apply
    regardless of these bounds — only metrics-driven (quant, risk)
    suggestions are eligible. The advisor adapter enforces that rule;
    the bounds here only constrain the magnitude of permitted changes
    once the role check passes.
    """

    enabled: bool = False
    max_spacing_change_percentage: Decimal = Field(
        default=Decimal("20"), ge=Decimal("0"), le=Decimal("100")
    )
    max_order_size_change_percentage: Decimal = Field(
        default=Decimal("15"), ge=Decimal("0"), le=Decimal("100")
    )

    class Config:
        frozen = True


class AdvisorConfig(BaseModel):
    """Top-level advisor settings.

    ``cadence_hours`` controls how often the engine invokes the
    advisor. The advisor's recommendations always persist to the
    ``advisor_suggestions`` table; whether they auto-apply is gated
    by ``auto_apply.enabled`` (and the news-role exclusion above).
    """

    type: AdvisorType = "single"
    cadence_hours: float = Field(default=4.0, gt=0)
    aggregator: AggregatorStrategy = "voting"
    arbitrator: ArbitratorConfig | None = None
    experts: list[ExpertConfig] = Field(default_factory=list)
    auto_apply: AutoApplyConfig = Field(default_factory=AutoApplyConfig)

    class Config:
        frozen = True

    @model_validator(mode="after")
    def _validate_moe_constraints(self) -> AdvisorConfig:
        """Enforce ADR-007 + ADR-009 advisor invariants."""
        if self.type == "moe":
            if len(self.experts) < 3:
                raise ValueError(
                    f"MoE advisor requires at least 3 experts; got {len(self.experts)}"
                )
            # Per-expert name uniqueness (collisions confuse aggregator logging)
            names = [e.name for e in self.experts]
            if len(set(names)) != len(names):
                duplicates = sorted({n for n in names if names.count(n) > 1})
                raise ValueError(f"expert names must be unique; duplicates: {duplicates}")
        elif self.type == "single" and self.experts:
            # Single-LLM mode shouldn't carry a populated experts list —
            # operator likely confused themselves
            raise ValueError(
                "advisor.type=single must not have experts (use type=moe for multi-expert setups)"
            )

        if self.aggregator == "arbitrator" and self.arbitrator is None:
            raise ValueError(
                "aggregator=arbitrator requires the `arbitrator:` config block to be set"
            )
        return self


__all__ = [
    "AdvisorConfig",
    "AdvisorType",
    "AggregatorStrategy",
    "ArbitratorConfig",
    "AutoApplyConfig",
    "ExpertConfig",
    "ExpertRole",
    "InferenceParams",
    "LLMProvider",
]

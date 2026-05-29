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

# Which decision engine drives the advisor (Stage 8.5):
# - ``llm``      — the LLM advisor (single or MoE), per ``type``. Pre-8.5
#                  behaviour; the default so existing configs are unchanged.
# - ``heuristic``— the deterministic ``HeuristicAdvisorAdapter`` only. $0,
#                  offline-safe; the LLM target fields are ignored.
# - ``cascade``  — heuristic first; escalate to the LLM only on an
#                  ambiguous (non-clear) call; fall back to the heuristic on
#                  LLM failure / cost-cap. Recommended for cloud-LLM setups.
AdvisorEngine = Literal["heuristic", "llm", "cascade"]


class InferenceParams(BaseModel):
    """Per-expert / per-arbitrator LLM inference knobs.

    Defaults are conservative; experts override per-role (the news
    expert might want higher temperature than the quant expert).

    ``timeout_seconds`` is the HTTP read timeout for the underlying
    provider call. 60s is plenty for 14B-class models. Bump to 180+
    for 70B+ models or thinking-style models (R1, o1, etc.) where
    the chain-of-thought phase adds latency.

    ``max_tokens`` bounds the model's output budget. For thinking
    models the budget must cover both the ``<think>...</think>``
    reasoning AND the JSON answer — 2048 is a sensible floor; 512
    is fine for non-thinking models.
    """

    temperature: Decimal = Field(default=Decimal("0.5"), ge=Decimal("0"), le=Decimal("2"))
    max_tokens: int = Field(default=512, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

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

    Advisor invocation cadence lives in the top-level ``schedules:``
    block as ``schedules.advise``. This block holds only the
    *what* of advisor configuration (which model, what prompt,
    aggregator, bounds); the *when* is unified with every other
    schedule.

    **Mode-specific fields:**
    - ``type=single`` requires ``provider``, ``model``, and
      ``prompt_file`` to be set (the single LLM has no
      ``ExpertConfig`` block to hide in). ``inference_params``
      defaults apply if not overridden.
    - ``type=moe`` populates ``experts`` (≥3) and ignores the
      single-mode fields. The MoE invariants (expert count, name
      uniqueness, arbitrator coupling) are enforced below.
    """

    type: AdvisorType = "single"

    # Decision engine (Stage 8.5). Defaults to ``llm`` so pre-8.5
    # configs behave exactly as before. ``cascade`` / ``heuristic``
    # require ``heuristic_file`` (validated below).
    engine: AdvisorEngine = "llm"

    # Path to the heuristic spec (curve + guard thresholds + toggles),
    # loaded at advisor-construction time like the prompt files.
    # Required when ``engine`` is ``heuristic`` or ``cascade``; ignored
    # for ``engine: llm``. See ``config/heuristic/quant.yml``.
    heuristic_file: str | None = Field(default=None, min_length=1)

    # Single-mode LLM target. Required when type=single; ignored
    # when type=moe. Mirrors the ExpertConfig fields so the operator
    # can lift-and-shift between modes without re-learning the
    # vocabulary.
    provider: LLMProvider | None = None
    model: str | None = Field(default=None, min_length=1)
    prompt_file: str | None = Field(default=None, min_length=1)
    inference_params: InferenceParams = Field(default_factory=InferenceParams)

    aggregator: AggregatorStrategy = "voting"
    arbitrator: ArbitratorConfig | None = None
    experts: list[ExpertConfig] = Field(default_factory=list)
    auto_apply: AutoApplyConfig = Field(default_factory=AutoApplyConfig)

    class Config:
        frozen = True

    @model_validator(mode="after")
    def _validate_mode_constraints(self) -> AdvisorConfig:
        """Enforce ADR-007 + ADR-009 + Stage 8.5 advisor invariants.

        The LLM-target checks (``type``-based) run only when the engine
        actually builds an LLM (``llm`` / ``cascade``); a pure
        ``heuristic`` engine ignores the provider/model/experts fields.
        ``cascade`` / ``heuristic`` additionally require ``heuristic_file``.
        """
        needs_llm = self.engine in ("llm", "cascade")
        needs_heuristic = self.engine in ("heuristic", "cascade")

        if needs_heuristic and self.heuristic_file is None:
            raise ValueError(
                f"advisor.engine={self.engine!r} requires `heuristic_file` "
                "(path to the heuristic spec, e.g. config/heuristic/quant.yml)"
            )

        if not needs_llm:
            # Pure heuristic engine — the LLM target fields are unused.
            return self

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
        elif self.type == "single":
            if self.experts:
                # Single-LLM mode shouldn't carry a populated experts list —
                # operator likely confused themselves
                raise ValueError(
                    "advisor.type=single must not have experts "
                    "(use type=moe for multi-expert setups)"
                )
            missing = [
                name
                for name, value in (
                    ("provider", self.provider),
                    ("model", self.model),
                    ("prompt_file", self.prompt_file),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"advisor.type=single requires {missing} to be set "
                    "(provider, model, prompt_file are the single-LLM target)"
                )

        if self.aggregator == "arbitrator" and self.arbitrator is None:
            raise ValueError(
                "aggregator=arbitrator requires the `arbitrator:` config block to be set"
            )
        return self


__all__ = [
    "AdvisorConfig",
    "AdvisorEngine",
    "AdvisorType",
    "AggregatorStrategy",
    "ArbitratorConfig",
    "AutoApplyConfig",
    "ExpertConfig",
    "ExpertRole",
    "InferenceParams",
    "LLMProvider",
]

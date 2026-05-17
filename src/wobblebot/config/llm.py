"""LLMConfig — top-level cloud-LLM cost + retry block (Phase 6 / ADR-014, ADR-015).

A small composite that bundles the two cross-cutting concerns Phase 6
introduces: cost caps (ADR-014) and retry/backoff policy (ADR-015).
Both apply to every cloud-LLM call regardless of which CLI made it,
which role drove it, or which provider answered — so a single block on
``WobbleBotConfig`` keeps the surface area minimal.

Per ADR-014 decision 5, ``WobbleBotConfig.llm`` is **Optional**:
``None`` = "no cloud usage; gate inactive." Pure-Ollama deployments
(the Phase 5 default posture) need zero config changes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wobblebot.services.llm_cost_gate import LLMCostConfig
from wobblebot.services.llm_retry import LLMRetryConfig


class LLMConfig(BaseModel):
    """Cross-cutting cloud-LLM settings (cost gate + retry/backoff).

    Both children carry their own defaults — operators only need to
    override fields they actually want to change. The block as a
    whole is optional on ``WobbleBotConfig``; setting it on is the
    opt-in signal that cloud usage is in scope.
    """

    cost: LLMCostConfig = Field(default_factory=LLMCostConfig)
    retry: LLMRetryConfig = Field(default_factory=LLMRetryConfig)

    class Config:
        frozen = True

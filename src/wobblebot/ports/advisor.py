"""AdvisorPort - Abstract interface for strategy recommendations.

This port defines the contract for LLM-based strategy advisory.
The Strategy Advisor module implements this port (Phase 3+).

CRITICAL: Advisor is advisory-only. It cannot execute trades or move funds.
All suggestions must be validated and gated by the Orchestrator.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp


class PerformanceSummary(BaseModel):
    """Sanitized performance summary sent to the Advisor.

    Contains no secrets, credentials, or sensitive data.
    """

    total_pnl: float = Field(..., description="Total realized + unrealized P&L")
    daily_pnl: float = Field(..., description="P&L for current day")
    win_rate: float = Field(..., ge=0, le=1, description="Fraction of winning trades")
    total_trades: int = Field(..., ge=0)
    active_positions: int = Field(..., ge=0)
    # Phase 3+ may add: volatility metrics, cycle counts, drawdown, etc.


class AdvisorRecommendation(BaseModel):
    """Recommendation from the Strategy Advisor.

    Must conform to strict JSON schema enforced at port boundary.
    All suggestions are bounded and validated before auto-application.
    """

    recommendation_id: str = Field(..., description="Unique recommendation ID")
    timestamp: Timestamp = Field(..., description="When the recommendation was issued")
    config_changes: dict[str, Any] = Field(
        default_factory=dict,
        description="Proposed configuration changes (key-value pairs)",
    )
    rationale: str = Field(..., description="Human-readable explanation")
    confidence: float = Field(..., ge=0, le=1, description="Confidence score (0-1)")

    # Schema validation enforced:
    # - Only whitelisted config fields allowed
    # - Values must be within configured min/max bounds
    # - Cannot violate safety constraints


class AdvisorPort(ABC):
    """Abstract interface for strategy advisor.

    Phase 3+ feature - provides LLM-based recommendations.

    Implementations:
    - Strategy Advisor (LLM adapter via Ollama or similar)

    Error convention:
    - Protocol/transport/validation failure raises ``AdvisorError``
      (LLM backend unreachable, JSON-schema validation fails, output
      violates configured safety bounds).
    """

    @abstractmethod
    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        """Request a strategy recommendation based on performance.

        Args:
            summary: Sanitized performance summary (no secrets)

        Returns:
            Recommendation with proposed config changes

        Raises:
            AdvisorError: If recommendation cannot be generated
            SchemaValidationError: If LLM output doesn't match schema
        """
        pass

    @abstractmethod
    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Validate a recommendation against safety rules.

        Args:
            recommendation: Recommendation to validate

        Returns:
            True if recommendation is safe to auto-apply

        Raises:
            ValidationError: If recommendation violates safety rules
        """
        pass

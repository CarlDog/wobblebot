"""AdvisorPort - Abstract interface for strategy recommendations.

This port defines the contract for LLM-based strategy advisory.
The Strategy Advisor module implements this port (Phase 3+).

CRITICAL: Advisor is advisory-only. It cannot execute trades or move funds.
All suggestions must be validated and gated by the Orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp

# Wire-level vocabulary for LLM-emitted recommendations. Mirrors what
# the prompt files in ``config/prompts/`` ask the model to produce.
ConfidenceLevel = Literal["high", "medium", "low"]


class CurrentGridParams(BaseModel):
    """Snapshot of the grid params the advisor's recommendations
    would modify. Sent alongside metrics so the LLM can reason about
    deltas, not absolute targets in a vacuum.

    All fields ``None`` is valid for the "no grid configured yet"
    case (e.g. running the advisor against pure observe data before
    any live engine has been wired up).
    """

    spacing_percentage: float | None = None
    levels_above: int | None = None
    levels_below: int | None = None
    order_size_usd: float | None = None

    class Config:
        frozen = True


class PerformanceSummary(BaseModel):
    """Sanitized snapshot of engine state sent to the Advisor.

    Contains no secrets, credentials, or sensitive data. Built from
    the DataCollector v2 surface (Stage 3.1 metrics) plus the
    engine's current grid config (Stage 2.2). LLM-friendly types
    (``float`` instead of ``Decimal``) since every field crosses the
    JSON boundary and the advisor's downstream consumers don't need
    monetary precision off this DTO.

    Attributes:
        symbol: Trading pair the summary covers (``"BTC/USD"``).
        lookback_hours: Window the metrics were computed over.
        latest_price: Most recent observed price in quote currency.
        snapshot_count: How many price snapshots fed the metrics —
            lets the LLM weight its confidence.
        volatility: Sample stdev of simple returns over the window.
        max_drawdown: Worst peak-to-trough fraction (``<= 0``).
        flatness: ``1 - range/mean`` clamped to ``[0, 1]``.
        cycle_count: Number of completed FIFO buy/sell cycles.
        win_rate: ``win_count / cycle_count``, or ``0`` if no cycles.
        total_pnl: Sum of realized cycle PnLs in quote currency.
        active_orders: How many orders are currently on the book.
        current_grid: Current grid params for context (delta-aware
            recommendations).
    """

    symbol: str = Field(min_length=3)
    lookback_hours: float = Field(gt=0)
    latest_price: float | None = None
    snapshot_count: int = Field(ge=0)
    volatility: float = Field(ge=0)
    max_drawdown: float = Field(le=0)
    flatness: float = Field(ge=0, le=1)
    cycle_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=1)
    total_pnl: float = 0.0
    active_orders: int = Field(ge=0, default=0)
    current_grid: CurrentGridParams = Field(default_factory=CurrentGridParams)

    class Config:
        frozen = True


class AdvisorRecommendation(BaseModel):
    """Recommendation from the Strategy Advisor (``advisor_recommendation_v1``).

    Wire-format mirror of what the prompt files in
    ``config/prompts/`` ask the LLM to emit. ``recommendation_id``
    and ``timestamp`` are populated by the adapter on receipt, not
    by the LLM. ``role`` identifies which expert produced the
    recommendation (``"single"`` for the Stage 3.2 single-LLM
    advisor; ``"quant"`` / ``"risk"`` / ``"news"`` for MoE experts
    in Stage 3.4a).

    The ``recommendations`` dict is intentionally loose at this
    layer — strict whitelisting happens at the auto-apply gate
    (Stage 3.4b), where the operator's ``auto_apply.*`` bounds
    decide which keys can mutate the running config. Stage 3.2 only
    parses and persists; nothing auto-applies.

    Attributes:
        recommendation_id: Adapter-generated unique ID.
        timestamp: When the adapter received this recommendation.
        role: Producing expert's role (``"single"`` outside MoE).
        recommendations: Proposed param changes — keys are config
            field names (e.g. ``"spacing_percentage"``), values are
            the proposed new values. Empty dict = "no change".
        rationale: Human-readable explanation from the LLM.
        confidence: LLM's self-reported confidence; the MoE
            aggregator (Stage 3.4a) translates the ordinal levels
            into weights.
    """

    recommendation_id: str = Field(min_length=1)
    timestamp: Timestamp
    role: str = Field(min_length=1)
    recommendations: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1)
    confidence: ConfidenceLevel

    class Config:
        frozen = True


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
            AdvisorError: If recommendation cannot be generated or
                the LLM output fails schema validation.
        """

    @abstractmethod
    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Validate a recommendation against safety rules.

        Args:
            recommendation: Recommendation to validate

        Returns:
            True if recommendation is safe to auto-apply

        Raises:
            AdvisorError: If recommendation violates safety rules
        """

"""Cost-tracking value object for cloud-LLM calls (Phase 6 / ADR-014).

One ``LLMCallRecord`` per cloud-LLM API call — success or failure.
Failed calls still record because some providers bill for them (e.g.
content-moderation refusals). Ollama (local, free) calls bypass this
machinery entirely; cost-tracking only kicks in for the three cloud
providers ADR-014 establishes.

The shape is deliberately Kraken-aligned to the trading-history models:
forensic write-once rows, never mutated after insert, indexed by
timestamp for the sliding-24h-window cost gate per ADR-014 decision 2.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp

LLMRole = Literal[
    "operator",  # operator-assistant role (Phase 5)
    "quant",  # MoE expert
    "risk",  # MoE expert
    "news",  # MoE expert
    "arbitrator",  # MoE arbitrator
    "single",  # single-LLM advisor (non-MoE path)
    "unknown",  # fallback for adapter contexts that can't classify
]

LLMProvider = Literal["anthropic", "openai", "google"]


class LLMCallRecord(BaseModel):
    """Forensic record of one cloud-LLM call.

    Attributes:
        id: Caller-minted UUID (default ``uuid4()``).
        timestamp: When the API request was issued.
        role: Which role drove the call (operator chat, MoE expert, etc.).
        provider: Cloud provider that received the request.
        model: Provider's model identifier (e.g. ``claude-sonnet-4-6``).
        tokens_in: Prompt token count from the provider's usage block.
        tokens_out: Completion token count.
        tokens_reasoning: Thinking-mode token count when the model
            exposes it (Anthropic extended thinking / OpenAI o-series /
            Gemini thinking). ``None`` for plain-completion calls.
        cost_usd: USD cost of this call computed locally from the
            pricing table (``services/llm_pricing.py``). 6-decimal
            precision is enough for any per-call charge at current
            provider rates.
        request_id: Provider-supplied correlation id for debugging
            (Anthropic ``request-id`` header, OpenAI ``id`` field,
            Google ``response_id``). ``None`` when the provider didn't
            return one or the call failed before the response arrived.
        success: Whether the call returned a usable response. Failed
            calls still get recorded because some providers bill.
        error_kind: Short label classifying the failure
            (``rate_limited``, ``timeout``, ``server_error``, etc.).
            ``None`` on success.
    """

    id: UUID = Field(default_factory=uuid4)
    timestamp: Timestamp
    role: LLMRole
    provider: LLMProvider
    model: str = Field(..., min_length=1)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    tokens_reasoning: int | None = Field(default=None, ge=0)
    cost_usd: Decimal = Field(..., ge=Decimal("0"))
    request_id: str | None = None
    success: bool
    error_kind: str | None = None

    class Config:
        frozen = True

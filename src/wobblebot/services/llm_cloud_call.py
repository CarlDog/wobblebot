"""Shared ADR-014/015 flow orchestrator for cloud-LLM adapters (Stage 6.3.A).

Every cloud-LLM adapter runs the same sequence per call:

  1. Estimate worst-case cost from prompt + max_tokens.
  2. Run the cost-gate ``check_budget`` (ADR-014); raise
     ``LLMCostCapExceeded`` on deny.
  3. Wrap the HTTP call in ``retry_with_backoff`` (ADR-015).
  4. On success: build an ``LLMCallRecord`` from the response's usage
     block, persist, update the session tracker; return the parsed
     envelope to the caller for provider-specific decoding.
  5. On permanent / transport / retry-exhausted failure: build a
     failure ``LLMCallRecord`` with classified ``error_kind``,
     persist (best-effort), re-raise.

This module captures steps 1-5 once. Per-provider adapters
(Anthropic / OpenAI / Google) supply the provider-specific bits via
two callables: ``call_fn`` (zero-arg async returning the response
envelope) and ``extract_tokens`` (envelope → tuple). The shared
helper composes them inside the cost-tracking sandwich.

**Why a function and not a base class.** The provider-specific
state lives on each adapter (api_key, base_url, model, etc.) which
the closures capture cleanly. A base class would either force those
fields onto a shared parent or push them through method overrides;
the closure approach keeps the cost-flow logic in one place and the
provider-shape logic next to the request body it builds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from wobblebot.adapters.ollama import OllamaJsonExtractError, extract_last_json_object
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.llm_cost import LLMCallRecord, LLMProvider, LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation
from wobblebot.ports.exceptions import AdvisorError, AssistantError, StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cost_gate import (
    GateDeny,
    LLMCostConfig,
    SessionCostTracker,
    check_budget,
)
from wobblebot.services.llm_pricing import cost_for
from wobblebot.services.llm_retry import LLMRetryConfig, retry_with_backoff

_LOGGER = logging.getLogger("wobblebot.services.llm_cloud_call")

# Type alias for the token-count tuple per-adapter callbacks return.
# (tokens_in, tokens_out, tokens_reasoning_or_None, request_id_or_None).
TokenTuple = tuple[int, int, int | None, str | None]
TokenExtractor = Callable[[dict[str, Any]], TokenTuple]


@dataclass(frozen=True)
class CloudCallContext:
    """Per-call cost-tracking deps + identity. Built once per adapter
    instance and reused across every call ``execute_cloud_call``
    receives."""

    storage: StoragePort
    session_tracker: SessionCostTracker
    cost_config: LLMCostConfig
    retry_config: LLMRetryConfig
    role: LLMRole
    provider: LLMProvider
    model: str


def classify_error(exc: Exception) -> str:
    """Short label for ``LLMCallRecord.error_kind`` on a failed call.

    Same shape as the per-adapter ``_classify_error`` Stage 6.2 had —
    promoted here so every cloud adapter labels failures consistently.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if 500 <= status < 600:
            return "server_error"
        return f"http_{status}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect_error"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timeout"
    return type(exc).__name__


def _make_failure_record(ctx: CloudCallContext, exc: Exception) -> LLMCallRecord:
    """Build the ``success=False`` record for a failed call."""
    return LLMCallRecord(
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=ctx.role,
        provider=ctx.provider,
        model=ctx.model,
        tokens_in=0,
        tokens_out=0,
        tokens_reasoning=None,
        cost_usd=Decimal("0"),
        request_id=None,
        success=False,
        error_kind=classify_error(exc),
    )


def _make_success_record(
    ctx: CloudCallContext,
    tokens: TokenTuple,
) -> LLMCallRecord:
    """Build the ``success=True`` record + compute cost from token counts."""
    tokens_in, tokens_out, tokens_reasoning, request_id = tokens
    cost = cost_for(
        provider=ctx.provider,
        model=ctx.model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=tokens_reasoning or 0,
    )
    return LLMCallRecord(
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=ctx.role,
        provider=ctx.provider,
        model=ctx.model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost,
        request_id=request_id,
        success=True,
        error_kind=None,
    )


async def _persist_best_effort(
    storage: StoragePort, record: LLMCallRecord, *, original_exc: Exception | None
) -> None:
    """Persist a record, swallowing StorageError + log it.

    Used for the failure path — losing the forensic row must not mask
    the original API exception to the caller. Success path uses the
    direct ``save_llm_call`` call because a storage failure there is
    a real bug we want to see.
    """
    try:
        await storage.save_llm_call(record)
    except StorageError as exc:
        _LOGGER.warning(
            "failed to persist failure record; original error will still raise",
            extra={
                "model": record.model,
                "provider": record.provider,
                "error_kind": record.error_kind,
                "storage_error": str(exc),
                "original_error": str(original_exc) if original_exc else None,
            },
        )


async def execute_cloud_call(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    ctx: CloudCallContext,
    estimated_cost_usd: Decimal,
    call_fn: Callable[[], Awaitable[dict[str, Any]]],
    extract_tokens: TokenExtractor,
) -> dict[str, Any]:
    """Run the full ADR-014/015 flow around one provider call.

    Args:
        ctx: Per-adapter cost-tracking deps + identity.
        estimated_cost_usd: Conservative ceiling per ADR-014 decision 4.
            Compute via ``services.llm_pricing.cost_for`` against the
            estimated input tokens + ``max_tokens`` ceiling.
        call_fn: Zero-arg async that performs the actual HTTP request.
            Must ``response.raise_for_status()`` so the retry helper
            sees structured ``HTTPStatusError`` for 4xx/5xx classification.
        extract_tokens: Pulls ``(tokens_in, tokens_out, tokens_reasoning,
            request_id)`` from the parsed response envelope. Per-provider
            normalization happens here (e.g. OpenAI subtracts
            reasoning_tokens from completion_tokens to satisfy the
            ``tokens_reasoning is additive to tokens_out`` convention).

    Returns:
        The parsed response envelope. Caller decodes provider-specific
        content (Anthropic content blocks / OpenAI choices / Google
        candidates) into its port's domain type.

    Raises:
        LLMCostCapExceeded: Cost gate denied the call.
        httpx.HTTPStatusError: Permanent HTTP failure (non-429 4xx).
        LLMRetryExhausted: Transient retries exhausted.
        Anything else ``call_fn`` raises that the classifier marks
        permanent.
    """
    decision = await check_budget(
        ctx.storage,
        role=ctx.role,
        estimated_cost_usd=estimated_cost_usd,
        session_spent_usd=ctx.session_tracker.total,
        config=ctx.cost_config,
    )
    if isinstance(decision, GateDeny):
        raise LLMCostCapExceeded(
            cap_kind=decision.cap_kind,
            cap_value_usd=decision.cap_value_usd,
            daily_spent_usd=decision.daily_spent_usd,
            session_spent_usd=decision.session_spent_usd,
            message=decision.reason,
        )

    try:
        envelope = await retry_with_backoff(call_fn, ctx.retry_config)
    except (httpx.HTTPError, Exception) as exc:  # pylint: disable=broad-exception-caught
        # Build + persist the failure record, then re-raise so the
        # caller decides how to surface it (typically wrapping as
        # AdvisorError / AssistantError with the cause chained).
        failure = _make_failure_record(ctx, exc)
        await _persist_best_effort(ctx.storage, failure, original_exc=exc)
        raise

    # Success path: extract tokens, build the record, persist, update
    # the session tracker. Storage failures here are real bugs — let
    # the StorageError bubble.
    tokens = extract_tokens(envelope)
    record = _make_success_record(ctx, tokens)
    await ctx.storage.save_llm_call(record)
    ctx.session_tracker.add(record.cost_usd)
    return envelope


# ===================================================================== #
# Shared response-parsing helpers (Stage 6.5.A close-audit extraction)  #
# ===================================================================== #
#
# Each cloud adapter pre-extracts the model's text from its
# provider-specific envelope shape (Anthropic content blocks /
# OpenAI choices / Google candidate parts). The text-to-dict and
# dict-to-domain-object steps below are identical across providers
# differing only in the provider name in error messages — promoted
# here at the Stage 6.5.A refactor pass.


def _parse_json_from_text(
    raw_text: str,
    *,
    provider_name: str,
    error_factory: Callable[[str], Exception],
) -> dict[str, Any]:
    """Walk ``raw_text`` for a JSON object; raise via ``error_factory``.

    The walk uses ``adapters.ollama.extract_last_json_object`` (the
    thinking-mode-aware extractor promoted in Stage 5.3). On extractor
    failure we fall back to ``json.loads`` for bare-JSON responses
    that don't need walking; on parse failure we wrap as the
    port-specific error type.

    ``error_factory`` is either ``AdvisorError`` or ``AssistantError``
    so each port's contract surfaces with its own type.
    """
    if not raw_text.strip():
        raise error_factory(f"{provider_name} response empty")
    try:
        return extract_last_json_object(raw_text)
    except OllamaJsonExtractError as exc:
        try:
            parsed: Any = json.loads(raw_text)
        except json.JSONDecodeError as json_exc:
            raise error_factory(str(exc)) from json_exc
        if not isinstance(parsed, dict):
            raise error_factory(
                f"{provider_name} response is JSON but not an object: " f"{type(parsed).__name__}"
            ) from exc
        return parsed


def parse_advisor_recommendation(
    raw_text: str,
    *,
    fallback_role: str,
    provider_name: str,
) -> AdvisorRecommendation:
    """Build an ``AdvisorRecommendation`` from raw LLM text.

    Shared across every cloud advisor adapter — three byte-identical
    copies pre-Stage-6.5.A. The provider-specific bit is the text
    extraction from the envelope (Anthropic / OpenAI / Google each
    have their own); after that the path is identical.

    Args:
        raw_text: The concatenated response text from the LLM.
        fallback_role: Role to record on the recommendation when the
            LLM omits ``role`` from its JSON.
        provider_name: For error messages — ``"Anthropic"`` /
            ``"OpenAI"`` / ``"Google"``.

    Raises:
        AdvisorError: Empty text, unparseable JSON, missing required
            field, or Pydantic validation failure.
    """
    inner = _parse_json_from_text(
        raw_text,
        provider_name=provider_name,
        error_factory=AdvisorError,
    )
    try:
        return AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role=str(inner.get("role", fallback_role)),
            recommendations=inner.get("recommendations") or {},
            rationale=str(inner.get("rationale", "")),
            confidence=inner["confidence"],
        )
    except KeyError as exc:
        raise AdvisorError(
            f"LLM output missing required field {exc.args[0]!r}; " f"got keys: {sorted(inner)}"
        ) from exc
    except ValidationError as exc:
        raise AdvisorError(
            f"LLM output failed advisor_recommendation_v1 schema validation: {exc}"
        ) from exc


def parse_intent_dict(
    raw_text: str,
    *,
    provider_name: str,
) -> dict[str, Any]:
    """Parse raw LLM text into a dict, raising ``AssistantError`` on failure.

    Caller (per-port adapter) runs the dict through its
    ``TypeAdapter[OperatorIntent]`` to validate against the
    operator_intent_v1 discriminated union — that validation stays at
    the call site since it requires the adapter's port-specific
    TypeAdapter import.
    """
    return _parse_json_from_text(
        raw_text,
        provider_name=provider_name,
        error_factory=AssistantError,
    )

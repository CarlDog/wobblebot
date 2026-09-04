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
envelope) and ``extract_tokens`` (envelope → ``TokenUsage``). The
shared helper composes them inside the cost-tracking sandwich.

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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.exceptions import LLMCostCapExceeded, LLMRetryExhausted
from wobblebot.domain.llm_cost import LLMCallRecord, LLMProvider, LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation
from wobblebot.ports.exceptions import AdvisorError, AssistantError, StorageError
from wobblebot.ports.operator import OperatorIntent
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cost_gate import (
    GateDeny,
    LLMCostConfig,
    SessionCostTracker,
    check_budget,
)
from wobblebot.services.llm_pricing import PricingLookupError, cost_for
from wobblebot.services.llm_retry import LLMRetryConfig, retry_with_backoff
from wobblebot.services.llm_trace import current_trace_id

_LOGGER = logging.getLogger("wobblebot.services.llm_cloud_call")


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """Token counts one provider call reported, as DISJOINT buckets.

    Every field is its own bucket — cost is Σ bucket × rate with no
    subtraction downstream (mirrors the pre-existing convention that
    ``tokens_reasoning`` is additive to ``tokens_out``). Extractors do
    the provider-specific normalization: OpenAI's ``prompt_tokens``
    INCLUDES its cached count (subtract there), Anthropic's
    ``input_tokens`` EXCLUDES its cache fields (passthrough), Gemini's
    ``promptTokenCount`` INCLUDES ``cachedContentTokenCount``
    (subtract there). See each provider's ``extract_*_tokens``.

    ``kw_only`` forces named construction so a transposed count can't
    slip through positionally — the reason this replaced the old
    4-tuple (``TokenTuple``) when the cache buckets landed (ADR-033).
    """

    tokens_in: int  # UNCACHED prompt tokens (full input price)
    tokens_out: int
    tokens_reasoning: int | None = None
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0  # Anthropic cache_creation only; 0 elsewhere
    request_id: str | None = None


TokenExtractor = Callable[[dict[str, Any]], TokenUsage]


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
        tokens_cache_read=0,
        tokens_cache_write=0,
        cost_usd=Decimal("0"),
        request_id=None,
        success=False,
        error_kind=classify_error(exc),
        trace_id=current_trace_id(),
    )


def _make_success_record(
    ctx: CloudCallContext,
    tokens: TokenUsage,
) -> LLMCallRecord:
    """Build the ``success=True`` record + compute cost from token counts."""
    cost = cost_for(
        provider=ctx.provider,
        model=ctx.model,
        tokens_in=tokens.tokens_in,
        tokens_out=tokens.tokens_out,
        tokens_reasoning=tokens.tokens_reasoning or 0,
        tokens_cache_read=tokens.tokens_cache_read,
        tokens_cache_write=tokens.tokens_cache_write,
    )
    return LLMCallRecord(
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=ctx.role,
        provider=ctx.provider,
        model=ctx.model,
        tokens_in=tokens.tokens_in,
        tokens_out=tokens.tokens_out,
        tokens_reasoning=tokens.tokens_reasoning,
        tokens_cache_read=tokens.tokens_cache_read,
        tokens_cache_write=tokens.tokens_cache_write,
        cost_usd=cost,
        request_id=tokens.request_id,
        success=True,
        error_kind=None,
        trace_id=current_trace_id(),
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
            "failed to persist failure record; original error will still raise (model=%s, "
            "provider=%s, error_kind=%s, storage_error=%s)",
            record.model,
            record.provider,
            record.error_kind,
            exc,
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
        extract_tokens: Pulls a :class:`TokenUsage` from the parsed
            response envelope. Per-provider normalization happens here
            (e.g. OpenAI subtracts reasoning_tokens from
            completion_tokens, and cached_tokens from prompt_tokens,
            to satisfy the disjoint-buckets convention).

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


# --------------------------------------------------------------------------- #
# JSON extraction — moved here from adapters/ollama.py on 2026-09-04.
#
# It lived in the Ollama adapter for historical reasons (that adapter needed
# it first) and every other consumer imported it from there — including THIS
# module, which made the sanctioned adapters->services seam bidirectional and
# left the package cycle-free only by accident. The function is pure text ->
# dict and its own docstring already called it port-agnostic, so services is
# where it belonged. The `Ollama` prefix on the error is retained only to
# avoid renaming 53 references across 8 files; it is not Ollama-specific.
# --------------------------------------------------------------------------- #


class OllamaJsonExtractError(Exception):
    """Internal helper exception — see :func:`extract_last_json_object`.

    Callers catch and re-raise as their port-specific error
    (``AdvisorError`` from the advisor adapter, ``AssistantError`` from
    the assistant adapter) so the shared helper stays port-agnostic.
    """


def extract_last_json_object(text: str) -> dict[str, Any]:
    """Walk ``text`` and return the last ``{...}`` block that parses as a JSON object.

    Thinking models emit a long reasoning preamble (``<think>...</think>``,
    bullet lists, code-fenced examples) before the final answer.
    ``json.JSONDecoder.raw_decode`` lets us advance from each ``{`` and
    try to parse a complete value from there — successful parses are
    collected and the last one wins.

    Shared between the advisor and assistant adapters; each wraps a
    failure as its port-specific error type.

    Args:
        text: The raw response body from the LLM.

    Returns:
        The parsed JSON object.

    Raises:
        OllamaJsonExtractError: If no parseable JSON object is present.
    """
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            try:
                obj, end_idx = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(obj, dict):
                candidates.append(obj)
            i = end_idx
        else:
            i += 1
    if not candidates:
        raise OllamaJsonExtractError(_no_json_message(text))
    return candidates[-1]


def _no_json_message(text: str) -> str:
    """Explain WHY no object parsed — truncation reads very differently.

    An opening ``{`` with nothing that closes it is the signature of a
    response cut off at the output-token cap, not of a model that
    ignored the schema. Those two failures need opposite responses
    (raise the cap vs. fix the prompt), and the old message — "no
    parseable JSON object in N chars" — described both identically.

    Cost a real diagnosis on 2026-08-10: three live cloud tests failed
    this way and read as provider drift; the actual cause was an output
    cap the prompt had outgrown, and for Gemini 2.5 a thinking budget
    that consumed 980 of 1024 tokens before any answer was emitted.
    """
    opener = text.find("{")
    if opener == -1:
        return (
            f"Model returned no JSON object at all in {len(text)} chars of output "
            f"(no '{{' present) — the response ignored the schema."
        )
    return (
        f"Model returned an UNTERMINATED JSON object in {len(text)} chars of output "
        f"— the response looks truncated. Raise the output-token cap; on "
        f"thinking-capable models the cap must cover reasoning AND the answer."
    )


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


def build_advisor_recommendation(
    inner: dict[str, Any],
    *,
    fallback_role: str,
) -> AdvisorRecommendation:
    """Build an :class:`AdvisorRecommendation` from an already-parsed dict.

    Split out of :func:`parse_advisor_recommendation` on 2026-09-04. That
    function does two things — text -> dict, then dict -> model — and only
    the FIRST is cloud-specific. ``adapters/ollama.py`` gets its dict from
    Ollama's ``format=json`` response instead of by extraction, so it could
    not reuse the whole helper and carried a byte-identical copy of this
    tail, error strings included. Two copies of a construction rule that
    decides what a malformed LLM response tells the operator is exactly the
    "2+ occurrences carrying a subtle correctness rule" the extraction bar
    names.

    Args:
        inner: The parsed JSON object from the model.
        fallback_role: Role recorded when the LLM omits ``role``.

    Raises:
        AdvisorError: Missing required field, or schema validation failure.
    """
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
    return build_advisor_recommendation(inner, fallback_role=fallback_role)


def parse_intent_dict(
    raw_text: str,
    *,
    provider_name: str,
) -> dict[str, Any]:
    """Parse raw LLM text into a dict, raising ``AssistantError`` on failure.

    Used internally by :func:`execute_assistant_call` and exposed for
    adapters that do their own dispatch (e.g. ``OllamaAssistantAdapter``
    which has a different transport-error shape).
    """
    return _parse_json_from_text(
        raw_text,
        provider_name=provider_name,
        error_factory=AssistantError,
    )


# ---------------------------------------------------------------------- #
# Shared OperatorIntent TypeAdapter + assistant call orchestrator         #
# ---------------------------------------------------------------------- #

# Module-level TypeAdapter — Pydantic discriminator resolution against
# ``OperatorIntent``'s two-level discriminated union is moderately
# expensive to set up. Construct once at import; reuse across every
# assistant adapter call site. Extracted 2026-05-23 from 4 verbatim
# copies that lived in each assistant adapter (audit finding #6).
INTENT_ADAPTER: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)


@asynccontextmanager
async def wrap_provider_errors(
    provider_name: str,
    error_cls: type[Exception],
) -> AsyncIterator[None]:
    """Translate provider transport + pricing-lookup failures to a port error.

    Every cloud-LLM adapter used to repeat the same 5-line pair of
    except clauses around its ``execute_cloud_call`` site. Audit
    finding #4-cloud surfaced 6 verbatim copies (3 providers × 2
    adapter classes). Centralizing here means a new provider adapter
    author can't forget either clause — they'd both stay missing
    until a test caught the leak.

    ``PricingLookupError`` is translated alongside the httpx errors so
    that a misconfigured / unpriced model is, from the caller's view,
    just another "the LLM cannot be called" condition. Without this the
    advise daemon crash-looped: a stale image missing an ``o3`` price
    entry raised ``PricingLookupError`` past the domain-error boundary,
    so neither ``_run_cycle`` (which catches ``AdvisorError``) nor the
    ``cascade`` heuristic fallback caught it. Compute the estimate
    *inside* this context manager (not before it) so the translation
    actually fires. The "fail loudly" intent (llm_pricing docstring) is
    preserved by the loud logs each consumer emits on the domain error.

    ``LLMRetryExhausted`` gets the same translation for the same reason
    (2026-08-05 outage): ``retry_with_backoff`` raises it directly (it
    is a ``WobbleBotDomainError``, not an ``httpx`` exception), so an
    ordinary transient failure -- an OpenAI 429 that outlasts the retry
    budget -- skipped this wrap entirely and killed the whole daemon the
    same way the ``PricingLookupError`` gap once did. 203 restarts in
    under two hours on a live deployment before this was caught.

    Args:
        provider_name: Display name for the error message (e.g.
            ``"Anthropic"``). Conventionally Title Case since it
            renders to operator-facing logs.
        error_cls: The port's domain error type — ``AdvisorError`` for
            advisor adapters, ``AssistantError`` for assistant
            adapters.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise error_cls(f"{provider_name} request failed: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise error_cls(f"{provider_name} transport error: {exc}") from exc
    except PricingLookupError as exc:
        raise error_cls(f"{provider_name} pricing unavailable: {exc}") from exc
    except LLMRetryExhausted as exc:
        raise error_cls(f"{provider_name} retries exhausted: {exc}") from exc


async def execute_assistant_call(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    ctx: CloudCallContext,
    estimate_cost_fn: Callable[[], Decimal],
    call_fn: Callable[[], Awaitable[dict[str, Any]]],
    extract_tokens: TokenExtractor,
    parse_text_fn: Callable[[dict[str, Any]], str],
    provider_name: str,
) -> OperatorIntent:
    """End-to-end execute + parse + validate for assistant adapters.

    Wraps :func:`execute_cloud_call` with the assistant-specific
    final-mile: extract the text payload via ``parse_text_fn``, parse
    the JSON via :func:`parse_intent_dict`, then validate against the
    shared :data:`INTENT_ADAPTER`. HTTP/transport errors get
    translated to :class:`AssistantError` via
    :func:`wrap_provider_errors`. Schema-validation failures land as
    ``AssistantError`` too with a stable message format.

    Extracted 2026-05-23 from 3 cloud assistant adapters (anthropic,
    openai, google) where the same 10-line tail block had drifted
    independently (audit finding #5). ``OllamaAssistantAdapter`` keeps
    its own dispatch because its transport-error shape differs (no
    HTTPStatusError vs HTTPError differentiation; local server).

    Args:
        ctx: Cost-context bundle (storage / tracker / cost+retry
            configs / role / provider / model).
        estimate_cost_fn: Zero-arg callable that calls
            :func:`estimate_cost_ceiling` for the gate check. Invoked
            *inside* :func:`wrap_provider_errors` (fleet-review #19
            finding 7) so an unpriced model's ``PricingLookupError``
            translates to ``AssistantError`` instead of escaping past
            every caller's error boundary and crash-looping the
            daemon — the exact failure this repo already fixed once
            on the advisor side (see ``adapters/anthropic.py``'s
            ``estimate_cost_ceiling`` call site).
        call_fn: Zero-arg async returning the provider's response
            envelope (already JSON-decoded). Typically a closure that
            calls the provider's ``post_*`` helper.
        extract_tokens: Provider-specific usage extractor returning a
            :class:`TokenUsage`.
        parse_text_fn: Provider-specific text-from-envelope extractor.
            Returns the raw model output the JSON parser should read.
        provider_name: Display name for error messages
            (``"Anthropic"`` / ``"OpenAI"`` / ``"Google"``).

    Returns:
        The validated ``OperatorIntent`` (one of the discriminated
        union variants).

    Raises:
        LLMCostCapExceeded: Cost gate denied the call.
        AssistantError: Transport failure, malformed envelope, JSON
            parse failure, or schema validation failure.
    """
    async with wrap_provider_errors(provider_name, AssistantError):
        estimated_cost_usd = estimate_cost_fn()
        envelope = await execute_cloud_call(
            ctx=ctx,
            estimated_cost_usd=estimated_cost_usd,
            call_fn=call_fn,
            extract_tokens=extract_tokens,
        )
    raw_text = parse_text_fn(envelope)
    inner = parse_intent_dict(raw_text, provider_name=provider_name)
    try:
        return INTENT_ADAPTER.validate_python(inner)
    except ValidationError as exc:
        raise AssistantError(
            f"LLM output failed operator_intent_v1 schema validation: {exc}"
        ) from exc

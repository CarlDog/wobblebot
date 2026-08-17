"""AnthropicAdvisorAdapter + shared Anthropic Messages-API helpers (Stage 6.2).

First real cloud-provider adapter under Phase 6. Implements ``AdvisorPort``
against Anthropic's Messages API; the sister :mod:`anthropic_assistant`
module (Stage 6.2.B) implements ``AssistantPort`` reusing the helpers
exported below.

**Wire format.** POSTs to ``/v1/messages`` with the system prompt
(prompt-file body) + a single user turn (the serialized
``PerformanceSummary`` + the "respond with JSON" reminder). Anthropic
returns content as a list of blocks; we concatenate the ``text`` blocks
and walk for the final ``{...}`` via
``adapters.ollama.extract_last_json_object`` (module-public since
Stage 5.3 — exactly the shape we need here).

**Cost-tracking.** Every call runs the full ADR-014 flow inside the
adapter:

  1. Estimate cost from ``len(prompt)//4 + max_tokens`` (conservative
     ceiling per ADR-014 decision 4 of the Stage 6.1 design doc).
  2. ``services.llm_cost_gate.check_budget`` against the per-session
     tracker + the storage-backed 24h window.
  3. If denied, raise ``LLMCostCapExceeded``. Otherwise proceed.
  4. ``services.llm_retry.retry_with_backoff`` wraps the actual HTTP
     call so ADR-015's transient/permanent classifier applies.
  5. On success, compute the actual cost from the response's
     ``usage.input_tokens`` + ``usage.output_tokens``, persist an
     ``LLMCallRecord``, and add the cost to the session tracker.
  6. On permanent failure, persist a ``success=False`` record with
     ``error_kind`` set, then re-raise as ``AdvisorError``.

**Anthropic thinking tokens.** v1 records ``tokens_reasoning=None`` —
Anthropic bills extended-thinking tokens at the same rate as regular
output tokens, and the API's ``usage`` block doesn't separate them
from ``output_tokens`` (you'd have to count tokens in the thinking
content blocks yourself). Cost is correct because the pricing fallback
treats reasoning at output rate. Operator-visible thinking-token
counts are a v2 candidate.

**Error wrapping.** Transport, HTTP-status, JSON-parse, and
Pydantic-validation failures wrap as ``AdvisorError`` with the original
exception chained. ``LLMCostCapExceeded`` (gate trip) bubbles
unchanged — it's its own domain error.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from wobblebot.config.prompts import Prompt
from wobblebot.domain.llm_cost import LLMRole
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cloud_call import (
    CloudCallContext,
    TokenUsage,
    execute_cloud_call,
    parse_advisor_recommendation,
    wrap_provider_errors,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import estimate_cost_ceiling
from wobblebot.services.llm_retry import LLMRetryConfig

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_API_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Anthropic DEPRECATED ``temperature`` starting with the Claude 5
# generation: sending it returns 400 ``invalid_request_error:
# `temperature` is deprecated for this model``. This is a hard failure
# on EVERY call, not a warning — measured 2026-08-10 against
# claude-sonnet-5 and claude-opus-5 (claude-haiku-4-5 still accepts it).
# Sibling of ``openai.is_reasoning_model``, which drops temperature for
# the o-series/gpt-5 family for the same reason.
#
# Parsing rather than an allowlist, so a model shipped tomorrow works
# without a code change. Ids look like ``claude-<tier>-<major>[-<minor>]``
# with an optional ``-YYYYMMDD`` snapshot suffix, so the MAJOR component
# is the generation: ``claude-haiku-4-5`` is 4.5 (keeps temperature) while
# ``claude-opus-5`` is 5 (drops it). Getting that boundary wrong in the
# other direction is what a naive "contains -5" check would do.
_MODEL_GENERATION_RE = re.compile(r"^claude-[a-z]+-(?P<major>\d+)(?:-(?P<minor>\d+))?(?:-\d{8})?$")
_TEMPERATURE_DEPRECATED_FROM_GENERATION = 5


def supports_temperature(model: str) -> bool:
    """Return True iff ``model`` still accepts a ``temperature`` field.

    Unparseable ids default to **True** (send it): an unrecognized id is
    far more likely to be a proxy/test alias for an older model than a
    future Claude generation, and the failure mode of guessing wrong
    here is a loud 400 rather than silent bad output.
    """
    match = _MODEL_GENERATION_RE.match(model)
    if match is None:
        return True
    return int(match.group("major")) < _TEMPERATURE_DEPRECATED_FROM_GENERATION


def parse_text_blocks(content: list[dict[str, Any]]) -> str:
    """Concatenate the ``text`` blocks from Anthropic's content array.

    Anthropic Messages API responses carry ``content`` as a list of
    block dicts, each with a ``type`` and additional fields. Stage 6.2
    handles two block types:

    - ``text`` — the actual answer text. Concatenated in order.
    - ``thinking`` — extended-thinking blocks (only present when the
      operator enabled thinking on the request). Ignored for response
      extraction — we walk the ``text`` content for the final JSON.

    Other block types (tool_use, image, etc.) are not in v1 scope.
    """
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def post_messages(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    api_version: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """One ``POST /v1/messages`` call. Raises ``httpx.HTTPStatusError``
    on non-2xx so the retry classifier sees a structured signal."""
    response = await client.post(
        f"{base_url}/v1/messages",
        json=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": api_version,
            "content-type": "application/json",
        },
    )
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


class AnthropicAdvisorAdapter(AdvisorPort):  # pylint: disable=too-many-instance-attributes
    """Anthropic-backed ``AdvisorPort`` implementation.

    Args:
        model: Anthropic model id (e.g. ``"claude-sonnet-4-6"``).
        prompt: Validated prompt file. Body becomes the system prompt;
            summary appended as JSON in the user turn.
        role: Value to use for ``AdvisorRecommendation.role`` if the
            LLM omits the field; also the role recorded against each
            ``LLMCallRecord``.
        api_key: Anthropic API key (from ``ANTHROPIC_API_KEY`` env).
        storage: Where to persist ``LLMCallRecord`` rows. The cost gate
            reads from the same store.
        session_tracker: In-memory running spend tally for the current
            CLI session. Shared across every adapter the CLI builds.
        cost_config: Per-day + per-session USD caps.
        retry_config: max_retries + backoff knobs.
        base_url: Override for tests / Anthropic-compatible proxies.
        api_version: ``anthropic-version`` header value.
        temperature: Sampling temperature.
        max_tokens: Hard cap on response tokens.
        timeout_seconds: HTTP read timeout.
        client: Optional pre-constructed ``httpx.AsyncClient`` (test
            seam). When ``None`` the adapter creates and owns one;
            ``aclose()`` releases it.
        prompt_caching: Send ``cache_control`` on the system prompt so
            calls sharing it within Anthropic's cache TTL bill the
            prefix at ~0.1x instead of 1x (ADR-033 amendment,
            2026-08-16 — ADR-033 shipped the accounting half; this is
            the sending half). Default ON: no deployed config path
            reaches Anthropic today, so in production the flag is
            inert, while the workloads that DO reach Anthropic — probe
            batteries and multi-symbol sweeps, both firing many calls
            sharing one system prompt within minutes — read the cache
            immediately. The losing case (single-call-per-TTL cadence
            paying the 1.25x write premium with no reads) applies only
            to a deployed 4h/12h single-symbol advisor, which is
            exactly the decision ADR-033 reserves for the operator.

            Two silent constraints, documented so nobody debugs them as
            failures: (1) the cacheable MINIMUM is model-dependent and
            not monotonic — 512 tokens on opus-5, 1024 on
            sonnet-5/opus-4.8, 4096 on haiku-4-5 — and a prefix under
            the floor silently doesn't cache (normal rate, no premium,
            ``cache_creation_input_tokens: 0``). wobblebot's role
            prompts run ~620-1050 tokens, so on haiku-4-5 this flag is
            currently a no-op by arithmetic. (2) Only the 5-minute TTL
            is ever sent: ADR-033's single
            ``cache_write_per_million_usd`` column models the 5m 1.25x
            premium, and the 1h TTL bills 2x — a ``ttl: "1h"`` would
            silently under-price every cache write in the ledger. Do
            not add a TTL knob without first adding a second pricing
            column.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        *,
        model: str,
        prompt: Prompt,
        role: LLMRole,
        api_key: str,
        storage: StoragePort,
        session_tracker: SessionCostTracker,
        cost_config: LLMCostConfig,
        retry_config: LLMRetryConfig,
        base_url: str = _DEFAULT_BASE_URL,
        api_version: str = _DEFAULT_API_VERSION,
        temperature: float = 0.5,
        max_tokens: int = 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        prompt_caching: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicAdvisorAdapter requires non-empty api_key")
        self._model = model
        self._prompt = prompt
        self._role: LLMRole = role
        self._api_key = api_key
        self._storage = storage
        self._session_tracker = session_tracker
        self._cost_config = cost_config
        self._retry_config = retry_config
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt_caching = prompt_caching
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Release the underlying httpx client if the adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def get_recommendation(
        self,
        summary: PerformanceSummary,
        *,
        extra_context: str = "",
    ) -> AdvisorRecommendation:
        user_message = (
            "Current engine state (JSON):\n\n"
            f"{summary.model_dump_json(indent=2)}\n\n"
            "Respond with JSON conforming to advisor_recommendation_v1."
        )
        if extra_context:
            user_message = f"{user_message}\n\n{extra_context}"

        # With caching on, the system prompt goes as a content block carrying
        # cache_control (bare "ephemeral" = the 5-minute TTL; never send
        # ttl="1h" — see the prompt_caching docstring). Prefixes under the
        # model's cacheable minimum are silently ignored by Anthropic, so
        # this is safe to send unconditionally.
        system: str | list[dict[str, Any]]
        if self._prompt_caching:
            system = [
                {
                    "type": "text",
                    "text": self._prompt.body,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system = self._prompt.body
        body: dict[str, Any] = {
            "model": self._model,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": self._max_tokens,
        }
        if supports_temperature(self._model):
            body["temperature"] = self._temperature

        async def _call() -> dict[str, Any]:
            return await post_messages(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                api_version=self._api_version,
                body=body,
            )

        full_prompt = f"{self._prompt.body}\n\n{user_message}"
        ctx = CloudCallContext(
            storage=self._storage,
            session_tracker=self._session_tracker,
            cost_config=self._cost_config,
            retry_config=self._retry_config,
            role=self._role,
            provider="anthropic",
            model=self._model,
        )
        # estimate_cost_ceiling runs INSIDE the wrap so a PricingLookupError
        # (unpriced model) surfaces as AdvisorError, not a daemon crash.
        async with wrap_provider_errors("Anthropic", AdvisorError):
            estimate = estimate_cost_ceiling(
                provider="anthropic",
                model=self._model,
                prompt_text=full_prompt,
                max_tokens=self._max_tokens,
            )
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_anthropic_tokens,
            )

        raw_text = parse_text_blocks(envelope.get("content", []) or [])
        return parse_advisor_recommendation(
            raw_text,
            fallback_role=self._role,
            provider_name="Anthropic",
        )

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Stage 3.2 contract: parsing-success is the only check."""
        del recommendation
        return True


def extract_anthropic_tokens(envelope: dict[str, Any]) -> TokenUsage:
    """Pull a ``TokenUsage`` from an Anthropic Messages-API response.

    Anthropic's usage shape::

        "usage": {
            "input_tokens": int,
            "output_tokens": int,
            "cache_creation_input_tokens": int,
            "cache_read_input_tokens": int
        }

    Unlike OpenAI/Gemini, ``input_tokens`` already EXCLUDES the cache
    fields — the three are disjoint on the wire, so all pass through
    unchanged. ``AnthropicAdvisorAdapter`` sends ``cache_control`` on its
    system prompt by default (2026-08-16 ADR-033 amendment), so the cache
    fields are live for advisor calls; the assistant adapter still never
    sends it (volatile system prefix), so its rows keep reading 0.

    Anthropic lumps extended-thinking tokens into ``output_tokens`` and
    bills them at the regular output rate, so v1 records
    ``tokens_reasoning=None``; the pricing fallback (see
    ``services/llm_pricing.cost_for``) covers it. Operator-visible
    thinking-token counts queued as a v2 candidate.
    """
    usage = envelope.get("usage", {}) or {}
    cache_read_raw = usage.get("cache_read_input_tokens", 0)
    cache_write_raw = usage.get("cache_creation_input_tokens", 0)
    return TokenUsage(
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        tokens_reasoning=None,
        tokens_cache_read=int(cache_read_raw) if cache_read_raw else 0,
        tokens_cache_write=int(cache_write_raw) if cache_write_raw else 0,
        request_id=envelope.get("id"),
    )

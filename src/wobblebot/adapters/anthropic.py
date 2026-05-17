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

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from wobblebot.adapters.ollama import (
    OllamaJsonExtractError,
    extract_last_json_object,
)
from wobblebot.config.prompts import Prompt
from wobblebot.domain.llm_cost import LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cloud_call import (
    CloudCallContext,
    TokenTuple,
    execute_cloud_call,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import cost_for
from wobblebot.services.llm_retry import LLMRetryConfig

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_API_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_SECONDS = 60.0


def estimate_cost_ceiling(
    *,
    model: str,
    prompt_text: str,
    max_tokens: int,
) -> Decimal:
    """Estimate the worst-case cost of an Anthropic call.

    Per ADR-014 decision 4 (Stage 6.1 design): the gate check sees a
    conservative upper bound, not the realistic mean — so the budget
    refuses anything that *could* tip over even if the actual response
    is cheaper.

    - Tokens in = ``len(prompt_text) // 4`` (the standard rule of
      thumb; Anthropic's actual tokenizer is BPE-like and within ~10%
      of this for English).
    - Tokens out = ``max_tokens`` (the model's hard ceiling).
    - Reasoning tokens = 0 (Anthropic lumps thinking with output;
      already covered by max_tokens).
    """
    tokens_in_est = max(1, len(prompt_text) // 4)
    return cost_for(
        provider="anthropic",
        model=model,
        tokens_in=tokens_in_est,
        tokens_out=max_tokens,
        tokens_reasoning=0,
    )


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

        body: dict[str, Any] = {
            "model": self._model,
            "system": self._prompt.body,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }

        async def _call() -> dict[str, Any]:
            return await post_messages(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                api_version=self._api_version,
                body=body,
            )

        full_prompt = f"{self._prompt.body}\n\n{user_message}"
        estimate = estimate_cost_ceiling(
            model=self._model,
            prompt_text=full_prompt,
            max_tokens=self._max_tokens,
        )
        ctx = CloudCallContext(
            storage=self._storage,
            session_tracker=self._session_tracker,
            cost_config=self._cost_config,
            retry_config=self._retry_config,
            role=self._role,
            provider="anthropic",
            model=self._model,
        )
        try:
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_anthropic_tokens,
            )
        except httpx.HTTPStatusError as exc:
            raise AdvisorError(
                f"Anthropic request failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdvisorError(f"Anthropic transport error: {exc}") from exc

        return _parse_recommendation(envelope=envelope, fallback_role=self._role)

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Stage 3.2 contract: parsing-success is the only check."""
        del recommendation
        return True


def extract_anthropic_tokens(envelope: dict[str, Any]) -> TokenTuple:
    """Pull the ``TokenTuple`` from an Anthropic Messages-API response.

    Anthropic lumps extended-thinking tokens into ``output_tokens`` and
    bills them at the regular output rate, so v1 records
    ``tokens_reasoning=None``; the pricing fallback (see
    ``services/llm_pricing.cost_for``) covers it. Operator-visible
    thinking-token counts queued as a v2 candidate.
    """
    usage = envelope.get("usage", {}) or {}
    return (
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        None,
        envelope.get("id"),
    )


def _parse_recommendation(
    *,
    envelope: dict[str, Any],
    fallback_role: str,
) -> AdvisorRecommendation:
    """Pull the JSON answer out of an Anthropic envelope + build an
    ``AdvisorRecommendation`` from it."""
    raw_text = parse_text_blocks(envelope.get("content", []) or [])
    if not raw_text.strip():
        raise AdvisorError(
            f"Anthropic response empty across content blocks; " f"envelope keys: {sorted(envelope)}"
        )

    # Walk for the final JSON object — handles thinking-mode preambles
    # + code-fenced examples + illustrative shapes in the prose.
    try:
        inner = extract_last_json_object(raw_text)
    except OllamaJsonExtractError as exc:
        # Fallback: maybe the model emitted bare JSON without prose.
        try:
            inner = json.loads(raw_text)
        except json.JSONDecodeError as json_exc:
            raise AdvisorError(str(exc)) from json_exc
        if not isinstance(inner, dict):
            raise AdvisorError(
                f"Anthropic response is JSON but not an object: {type(inner).__name__}"
            ) from exc

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

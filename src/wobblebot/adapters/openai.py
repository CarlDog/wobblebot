"""OpenAI adapters — Stage 6.3.B (Phase 6 / ADR-014 + ADR-015).

Sister module to ``adapters/anthropic.py``. Implements both
``AdvisorPort`` (``OpenAIAdvisorAdapter``) and ``AssistantPort``
(``OpenAIAssistantAdapter``) against OpenAI's Chat Completions API
endpoint (``/v1/chat/completions``).

**Wire format.** POSTs JSON body of shape::

    {
        "model": "<id>",
        "messages": [
            {"role": "system", "content": <system prompt>},
            {"role": "user", "content": <...>},
            ...
        ],
        "max_completion_tokens": <int>,
        "temperature": <float>,   # omitted for o-series
    }

OpenAI uses ``Authorization: Bearer <api_key>`` (not the Anthropic
``x-api-key`` header) plus the optional ``OpenAI-Organization`` header
when an org id is supplied.

**Reasoning-token normalization.** OpenAI's o-series models
(``o1``, ``o3-mini``, future) return both ``completion_tokens`` AND
``completion_tokens_details.reasoning_tokens`` in the usage block —
and ``completion_tokens`` is the SUM (reasoning is a subset of
completion). Per the ``services/llm_pricing`` convention that
``tokens_reasoning`` is **additive** to ``tokens_out``, the adapter
subtracts reasoning from completion so the recorded columns satisfy
``tokens_out + tokens_reasoning = total billable output``. Cost
math via ``cost_for`` works correctly because reasoning falls back
to the output rate (which is how OpenAI bills o-series reasoning).

**Cost-tracking + retry.** All ADR-014/015 flow lives in
``services.llm_cloud_call.execute_cloud_call``; each adapter just
supplies its ``call_fn`` closure + the OpenAI-specific
``extract_openai_tokens`` extractor.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from wobblebot.config.prompts import Prompt
from wobblebot.domain.llm_cost import LLMRole
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.assistant import AssistantPort, ConversationContext
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.ports.operator import OperatorIntent
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cloud_call import (
    CloudCallContext,
    TokenTuple,
    execute_cloud_call,
    parse_advisor_recommendation,
    parse_intent_dict,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import estimate_cost_ceiling
from wobblebot.services.llm_retry import LLMRetryConfig

_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Model-id prefixes that identify OpenAI's reasoning-tuned models.
# These models accept `reasoning_effort` and ignore `temperature`;
# their `usage` block always includes `completion_tokens_details`.
_REASONING_MODEL_PREFIXES = ("o1", "o3")

# Module-level TypeAdapter — cheap to construct once, expensive to
# rebuild per call. Materializes the operator_intent_v1 discriminated
# union on the assistant path.
_INTENT_ADAPTER: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)


def is_reasoning_model(model: str) -> bool:
    """Return True iff the OpenAI model id is an o-series reasoning model.

    Used to:
    - Drop ``temperature`` from the request body (o-series ignores it
      and the API rejects it on some endpoints).
    - Use ``max_completion_tokens`` (preferred for o-series; legacy
      ``max_tokens`` works for chat models too — we always use
      ``max_completion_tokens`` for forward compatibility).
    """
    name = model.lower()
    return any(name.startswith(p) for p in _REASONING_MODEL_PREFIXES)


def extract_openai_tokens(envelope: dict[str, Any]) -> TokenTuple:
    """Pull ``TokenTuple`` from an OpenAI Chat Completions response.

    OpenAI's usage shape (o-series):
        {
            "prompt_tokens": int,
            "completion_tokens": int,
            "completion_tokens_details": {"reasoning_tokens": int, ...},
            "total_tokens": int
        }

    Chat models without reasoning omit ``completion_tokens_details`` or
    report ``reasoning_tokens=0`` — either way the normalization is:

        tokens_reasoning = details.reasoning_tokens (or None if absent)
        tokens_out = completion_tokens - reasoning_tokens

    so the recorded columns satisfy the additive convention from
    ``services/llm_pricing``. Cost math through ``cost_for`` then
    applies output rate to both (reasoning_per_million_usd=None for
    OpenAI o-series, which falls back to output rate — exactly how
    OpenAI bills).
    """
    usage = envelope.get("usage", {}) or {}
    tokens_in = int(usage.get("prompt_tokens", 0))
    total_completion = int(usage.get("completion_tokens", 0))
    details = usage.get("completion_tokens_details") or {}
    reasoning_raw = details.get("reasoning_tokens", 0)
    reasoning = int(reasoning_raw) if reasoning_raw else 0
    # Defensive: if the provider ever reports reasoning > completion
    # (shouldn't happen — they bill on completion which is the sum),
    # clamp to avoid a negative tokens_out.
    tokens_out = max(0, total_completion - reasoning)
    tokens_reasoning = reasoning if reasoning > 0 else None
    return (tokens_in, tokens_out, tokens_reasoning, envelope.get("id"))


def parse_message_content(envelope: dict[str, Any]) -> str:
    """Concatenate the message content from the first choice.

    OpenAI returns ``choices: [{message: {role, content}, ...}, ...]``.
    We use the first choice (only one when ``n`` is unset / defaulted
    to 1, which we always do). ``content`` is normally a string; for
    multimodal / tool-use responses it can be a list of parts, but
    Phase 6 doesn't enable those request shapes.
    """
    choices = envelope.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal/tool shape — concatenate text parts only.
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


async def post_chat_completion(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    organization: str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    """One ``POST /v1/chat/completions`` call. Raises
    ``httpx.HTTPStatusError`` on non-2xx so the retry classifier
    sees structured signal."""
    headers: dict[str, str] = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    if organization:
        headers["openai-organization"] = organization
    response = await client.post(
        f"{base_url}/v1/chat/completions",
        json=body,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def _build_chat_body(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Compose the Chat Completions request body, omitting fields the
    target model doesn't accept (e.g. ``temperature`` for o-series)."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if not is_reasoning_model(model):
        body["temperature"] = temperature
    return body


# ===================================================================== #
# AdvisorPort implementation                                            #
# ===================================================================== #


class OpenAIAdvisorAdapter(AdvisorPort):  # pylint: disable=too-many-instance-attributes
    """OpenAI-backed ``AdvisorPort`` implementation.

    Args:
        model: OpenAI model id (e.g. ``"gpt-4o"``, ``"o1"``).
        prompt: Validated prompt file.
        role: ``LLMCallRecord.role`` for this adapter's calls + fallback
            for ``AdvisorRecommendation.role`` when the LLM omits it.
        api_key: OpenAI API key (from ``OPENAI_API_KEY`` env).
        organization: Optional ``OpenAI-Organization`` header; from
            ``OPENAI_ORGANIZATION`` env if set, else ``None``.
        storage / session_tracker / cost_config / retry_config: see
            ``services/llm_cloud_call.CloudCallContext``.
        base_url: Override for tests / OpenAI-compatible proxies.
        temperature: Sampling temperature (ignored for o-series).
        max_tokens: Hard cap on output tokens.
        timeout_seconds: HTTP read timeout.
        client: Optional pre-constructed ``httpx.AsyncClient`` (test
            seam).
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
        organization: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.5,
        max_tokens: int = 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIAdvisorAdapter requires non-empty api_key")
        self._model = model
        self._prompt = prompt
        self._role: LLMRole = role
        self._api_key = api_key
        self._organization = organization
        self._storage = storage
        self._session_tracker = session_tracker
        self._cost_config = cost_config
        self._retry_config = retry_config
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
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

        messages = [
            {"role": "system", "content": self._prompt.body},
            {"role": "user", "content": user_message},
        ]
        body = _build_chat_body(
            model=self._model,
            messages=messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        async def _call() -> dict[str, Any]:
            return await post_chat_completion(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                organization=self._organization,
                body=body,
            )

        full_prompt = f"{self._prompt.body}\n\n{user_message}"
        estimate = estimate_cost_ceiling(
            provider="openai",
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
            provider="openai",
            model=self._model,
        )
        try:
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_openai_tokens,
            )
        except httpx.HTTPStatusError as exc:
            raise AdvisorError(f"OpenAI request failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise AdvisorError(f"OpenAI transport error: {exc}") from exc

        raw_text = parse_message_content(envelope)
        return parse_advisor_recommendation(
            raw_text,
            fallback_role=self._role,
            provider_name="OpenAI",
        )

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True


# ===================================================================== #
# AssistantPort implementation                                          #
# ===================================================================== #


class OpenAIAssistantAdapter(AssistantPort):  # pylint: disable=too-many-instance-attributes
    """OpenAI-backed ``AssistantPort`` for the operator interaction layer.

    Sister to ``OpenAIAdvisorAdapter``; reuses every helper above.
    Composes the operator-prompt system message + role-tagged turn
    history + current operator message into the Chat Completions
    payload. Records every call as ``role="operator"`` in the cost
    ledger.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        *,
        model: str,
        prompt: Prompt,
        api_key: str,
        storage: StoragePort,
        session_tracker: SessionCostTracker,
        cost_config: LLMCostConfig,
        retry_config: LLMRetryConfig,
        organization: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.3,
        max_tokens: int = 512,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIAssistantAdapter requires non-empty api_key")
        if prompt.metadata.role != "operator":
            raise AssistantError(
                f"OpenAIAssistantAdapter requires an operator-role prompt; "
                f"got role={prompt.metadata.role!r}"
            )
        self._model = model
        self._prompt = prompt
        self._api_key = api_key
        self._organization = organization
        self._storage = storage
        self._session_tracker = session_tracker
        self._cost_config = cost_config
        self._retry_config = retry_config
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def parse_intent(self, context: ConversationContext) -> OperatorIntent:
        system_content = (
            f"{self._prompt.body}\n\n"
            "Current engine state (JSON):\n"
            f"{context.engine_state_snapshot.model_dump_json(indent=2)}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        for turn in context.recent_turns:
            messages.append(
                {
                    "role": "user" if turn.role == "operator" else "assistant",
                    "content": turn.content,
                }
            )
        messages.append({"role": "user", "content": context.current_message})

        prompt_text = "\n".join(m["content"] for m in messages)
        estimate = estimate_cost_ceiling(
            provider="openai",
            model=self._model,
            prompt_text=prompt_text,
            max_tokens=self._max_tokens,
        )
        body = _build_chat_body(
            model=self._model,
            messages=messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        async def _call() -> dict[str, Any]:
            return await post_chat_completion(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                organization=self._organization,
                body=body,
            )

        ctx = CloudCallContext(
            storage=self._storage,
            session_tracker=self._session_tracker,
            cost_config=self._cost_config,
            retry_config=self._retry_config,
            role="operator",
            provider="openai",
            model=self._model,
        )
        try:
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_openai_tokens,
            )
        except httpx.HTTPStatusError as exc:
            raise AssistantError(f"OpenAI request failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise AssistantError(f"OpenAI transport error: {exc}") from exc

        raw_text = parse_message_content(envelope)
        inner = parse_intent_dict(raw_text, provider_name="OpenAI")
        try:
            return _INTENT_ADAPTER.validate_python(inner)
        except ValidationError as exc:
            raise AssistantError(
                f"LLM output failed operator_intent_v1 schema validation: {exc}"
            ) from exc

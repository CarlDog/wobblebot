"""Google Gemini adapters — Stage 6.4 (Phase 6 / ADR-014 + ADR-015).

Third cloud provider; lands on the same shared ``execute_cloud_call``
orchestrator Stage 6.3.A extracted. Implements both ``AdvisorPort``
(``GoogleAdvisorAdapter``) and ``AssistantPort``
(``GoogleAssistantAdapter``) against Google Generative AI's REST API
(``generativelanguage.googleapis.com``). Vertex AI is out of scope —
the hobby-tier Gemini API is sufficient for Phase 6 and avoids the
OAuth + GCP-project ceremony Vertex requires.

**Wire format.** POSTs JSON to
``/v1beta/models/{model}:generateContent``. Body shape::

    {
        "systemInstruction": {"parts": [{"text": "<system>"}]},
        "contents": [
            {"role": "user",  "parts": [{"text": "<...>"}]},
            {"role": "model", "parts": [{"text": "<...>"}]},
            ...
        ],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 1024
        }
    }

Authentication uses the ``x-goog-api-key`` header (preferred since the
v1beta API; query-string ``?key=`` works too but pollutes URL-shaped
logs). Notable role-vocabulary quirk: Google uses ``"model"`` (not
``"assistant"``) in ``contents``, so the operator-assistant turn
mapping translates ``assistant`` → ``model`` on the wire.

**Reasoning-token normalization is the simplest of the three Phase 6
providers.** Gemini's usage shape exposes ``thoughtsTokenCount`` as a
SEPARATE field, **additive** to ``candidatesTokenCount`` (unlike
OpenAI which had to subtract). The extractor records both as-is.

**Cost-tracking + retry** flow comes from
``services.llm_cloud_call.execute_cloud_call``; each adapter just
supplies its provider-specific ``call_fn`` closure + the
``extract_google_tokens`` extractor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

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
from wobblebot.ports.assistant import AssistantPort, ConversationContext
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.ports.operator import OperatorIntent
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cloud_call import (
    CloudCallContext,
    TokenTuple,
    execute_cloud_call,
)
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import cost_for
from wobblebot.services.llm_retry import LLMRetryConfig

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Module-level TypeAdapter — Pydantic discriminator resolution is the
# only way to materialize the right OperatorIntent variant. Cheap to
# construct once.
_INTENT_ADAPTER: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)


def estimate_cost_ceiling(
    *,
    model: str,
    prompt_text: str,
    max_tokens: int,
) -> Decimal:
    """Conservative cost ceiling for a Gemini call (ADR-014 decision 4).

    Same shape as the Anthropic + OpenAI estimators. Input tokens
    approximated at ``len/4``; output ceiling = ``max_tokens``. For
    Gemini 2.5 thinking models the runtime ``thoughtsTokenCount``
    isn't predictable in advance — the conservative posture folds
    whatever reasoning emerges into the output budget at the (lower)
    output rate. Gemini-flash thinking actually bills thoughts at a
    HIGHER rate than regular output, so post-hoc the actual cost may
    exceed the estimate; ADR-014 decision 4 acknowledges the gate's
    estimate is a conservative *under*-bound for thinking-mode calls,
    not a hard upper bound — the daily-cap window catches accumulated
    overshoot.
    """
    tokens_in_est = max(1, len(prompt_text) // 4)
    return cost_for(
        provider="google",
        model=model,
        tokens_in=tokens_in_est,
        tokens_out=max_tokens,
        tokens_reasoning=0,
    )


def extract_google_tokens(envelope: dict[str, Any]) -> TokenTuple:
    """Pull ``TokenTuple`` from a Gemini ``generateContent`` response.

    Gemini's usage shape::

        "usageMetadata": {
            "promptTokenCount": int,
            "candidatesTokenCount": int,
            "thoughtsTokenCount": int (gemini-2.5+ thinking only),
            "totalTokenCount": int
        }

    ``thoughtsTokenCount`` is **additive** to ``candidatesTokenCount``
    — no subtraction needed (unlike OpenAI). Older models without
    thinking-mode omit the field; we treat absent / zero as ``None``
    so the database column doesn't carry signal-free zeros.

    Top-level ``responseId`` is the provider correlation id when
    present; older responses omit it.
    """
    usage = envelope.get("usageMetadata", {}) or {}
    tokens_in = int(usage.get("promptTokenCount", 0))
    tokens_out = int(usage.get("candidatesTokenCount", 0))
    thoughts_raw = usage.get("thoughtsTokenCount", 0)
    thoughts = int(thoughts_raw) if thoughts_raw else 0
    tokens_reasoning = thoughts if thoughts > 0 else None
    request_id = envelope.get("responseId")
    return (tokens_in, tokens_out, tokens_reasoning, request_id)


def parse_candidate_text(envelope: dict[str, Any]) -> str:
    """Concatenate the ``text`` parts from the first candidate's content.

    Gemini returns ``candidates: [{content: {parts: [{text: "..."}, ...]}, ...}]``.
    We use the first candidate (only one when ``candidateCount`` is unset,
    which we always do). Non-text parts (inlineData, executableCode,
    etc.) are not in v1 scope and get filtered out.
    """
    candidates = envelope.get("candidates") or []
    if not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, dict):
        return ""
    content = first.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts") or []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


async def post_generate_content(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """One ``POST /v1beta/models/{model}:generateContent`` call.

    Raises ``httpx.HTTPStatusError`` on non-2xx so the retry classifier
    sees structured signal.
    """
    url = f"{base_url}/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "content-type": "application/json",
    }
    response = await client.post(url, json=body, headers=headers)
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def _build_generate_body(
    *,
    system_text: str,
    contents: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Compose the generateContent request body."""
    return {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }


def _user_part(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def _model_part(text: str) -> dict[str, Any]:
    """Gemini uses ``"model"`` (not ``"assistant"``) for assistant turns."""
    return {"role": "model", "parts": [{"text": text}]}


# ===================================================================== #
# AdvisorPort implementation                                            #
# ===================================================================== #


class GoogleAdvisorAdapter(AdvisorPort):  # pylint: disable=too-many-instance-attributes
    """Google Gemini-backed ``AdvisorPort`` implementation.

    Args:
        model: Gemini model id (e.g. ``"gemini-2.5-pro"``,
            ``"gemini-2.5-flash"``).
        prompt: Validated prompt file.
        role: ``LLMCallRecord.role`` + fallback for
            ``AdvisorRecommendation.role``.
        api_key: Gemini API key (from ``GOOGLE_API_KEY`` env).
        storage / session_tracker / cost_config / retry_config: see
            ``services/llm_cloud_call.CloudCallContext``.
        base_url: Override for tests.
        temperature: Sampling temperature.
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
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.5,
        max_tokens: int = 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GoogleAdvisorAdapter requires non-empty api_key")
        self._model = model
        self._prompt = prompt
        self._role: LLMRole = role
        self._api_key = api_key
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

        body = _build_generate_body(
            system_text=self._prompt.body,
            contents=[_user_part(user_message)],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        async def _call() -> dict[str, Any]:
            return await post_generate_content(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                model=self._model,
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
            provider="google",
            model=self._model,
        )
        try:
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_google_tokens,
            )
        except httpx.HTTPStatusError as exc:
            raise AdvisorError(f"Google request failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise AdvisorError(f"Google transport error: {exc}") from exc

        return _parse_recommendation(envelope=envelope, fallback_role=self._role)

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True


# ===================================================================== #
# AssistantPort implementation                                          #
# ===================================================================== #


class GoogleAssistantAdapter(AssistantPort):  # pylint: disable=too-many-instance-attributes
    """Google Gemini-backed ``AssistantPort`` for the operator layer.

    Sister to ``GoogleAdvisorAdapter``; same wire helpers, same
    cost-tracking flow via ``execute_cloud_call``. Maps the operator's
    conversation history into Gemini's ``contents`` array:
    ``operator`` turns become ``user``, ``assistant`` turns become
    ``"model"`` (Gemini's role vocabulary).
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
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.3,
        max_tokens: int = 512,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GoogleAssistantAdapter requires non-empty api_key")
        if prompt.metadata.role != "operator":
            raise AssistantError(
                f"GoogleAssistantAdapter requires an operator-role prompt; "
                f"got role={prompt.metadata.role!r}"
            )
        self._model = model
        self._prompt = prompt
        self._api_key = api_key
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

    async def parse_intent(  # pylint: disable=too-many-locals
        self, context: ConversationContext
    ) -> OperatorIntent:
        system_text = (
            f"{self._prompt.body}\n\n"
            "Current engine state (JSON):\n"
            f"{context.engine_state_snapshot.model_dump_json(indent=2)}"
        )
        contents: list[dict[str, Any]] = []
        for turn in context.recent_turns:
            if turn.role == "operator":
                contents.append(_user_part(turn.content))
            else:
                contents.append(_model_part(turn.content))
        contents.append(_user_part(context.current_message))

        # Estimate uses the concatenated text length (system + every
        # part) — same shape as the sister adapters.
        per_message_text = []
        for c in contents:
            for part in c.get("parts", []):
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if isinstance(text, str):
                        per_message_text.append(text)
        prompt_text = system_text + "\n\n" + "\n".join(per_message_text)
        estimate = estimate_cost_ceiling(
            model=self._model,
            prompt_text=prompt_text,
            max_tokens=self._max_tokens,
        )

        body = _build_generate_body(
            system_text=system_text,
            contents=contents,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        async def _call() -> dict[str, Any]:
            return await post_generate_content(
                client=self._client,
                base_url=self._base_url,
                api_key=self._api_key,
                model=self._model,
                body=body,
            )

        ctx = CloudCallContext(
            storage=self._storage,
            session_tracker=self._session_tracker,
            cost_config=self._cost_config,
            retry_config=self._retry_config,
            role="operator",
            provider="google",
            model=self._model,
        )
        try:
            envelope = await execute_cloud_call(
                ctx=ctx,
                estimated_cost_usd=estimate,
                call_fn=_call,
                extract_tokens=extract_google_tokens,
            )
        except httpx.HTTPStatusError as exc:
            raise AssistantError(f"Google request failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise AssistantError(f"Google transport error: {exc}") from exc

        inner = _extract_intent_dict(envelope)
        try:
            return _INTENT_ADAPTER.validate_python(inner)
        except ValidationError as exc:
            raise AssistantError(
                f"LLM output failed operator_intent_v1 schema validation: {exc}"
            ) from exc


# ===================================================================== #
# Internal parse helpers                                                #
# ===================================================================== #


def _parse_recommendation(
    *,
    envelope: dict[str, Any],
    fallback_role: str,
) -> AdvisorRecommendation:
    """Pull the JSON answer out of a Gemini envelope + build an
    ``AdvisorRecommendation``."""
    raw_text = parse_candidate_text(envelope)
    if not raw_text.strip():
        raise AdvisorError(
            f"Google response empty across candidates; " f"envelope keys: {sorted(envelope)}"
        )
    try:
        inner = extract_last_json_object(raw_text)
    except OllamaJsonExtractError as exc:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as json_exc:
            raise AdvisorError(str(exc)) from json_exc
        if not isinstance(parsed, dict):
            raise AdvisorError(
                f"Google response is JSON but not an object: {type(parsed).__name__}"
            ) from exc
        inner = parsed

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


def _extract_intent_dict(envelope: dict[str, Any]) -> dict[str, Any]:
    raw_text = parse_candidate_text(envelope)
    if not raw_text.strip():
        raise AssistantError(
            f"Google response empty across candidates; " f"envelope keys: {sorted(envelope)}"
        )
    try:
        return extract_last_json_object(raw_text)
    except OllamaJsonExtractError as exc:
        try:
            parsed: Any = json.loads(raw_text)
        except json.JSONDecodeError as json_exc:
            raise AssistantError(str(exc)) from json_exc
        if not isinstance(parsed, dict):
            raise AssistantError(
                f"Google response is JSON but not an object: {type(parsed).__name__}"
            ) from exc
        return parsed

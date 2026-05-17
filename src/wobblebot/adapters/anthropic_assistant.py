"""AnthropicAssistantAdapter — Stage 6.2.B ``AssistantPort`` backed by Anthropic.

Sister adapter to :mod:`wobblebot.adapters.anthropic` (which implements
``AdvisorPort`` for the trading-advisor path in Stage 6.2.A). Same
Anthropic Messages API infrastructure, different port: this adapter
takes a ``ConversationContext`` (current operator message + recent
turn history + engine state snapshot) and returns a typed
``OperatorIntent``.

**Wire format.** POSTs to ``/v1/messages``. The system prompt is set
via the top-level ``system`` field. Recent turns + the current
operator message ride as a list under ``messages`` with role tags
``user`` / ``assistant`` — Anthropic's Messages API spec.

**Shared with the advisor adapter.** ``estimate_cost_ceiling``,
``parse_text_blocks``, ``build_call_record``, ``post_messages`` from
``adapters/anthropic.py``. Also reuses ``extract_last_json_object``
from ``adapters/ollama`` (module-public since Stage 5.3).

**Cost-tracking.** Same flow as the advisor adapter: estimate →
``check_budget`` → ``retry_with_backoff(post_messages)`` → persist
``LLMCallRecord`` + update tracker. Cost gate uses the operator role
(``role="operator"``).

**Output validation.** The LLM's JSON is validated against the
``OperatorIntent`` discriminated-union ``TypeAdapter`` (same two-level
discriminator resolution Stage 5.3 established for the Ollama
assistant adapter).

**Per ADR-013** this adapter is NOT in the money path. An
``AssistantError`` only affects the Discord chat surface; ``cli/live``
never imports this module.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from wobblebot.adapters.anthropic import (
    build_call_record,
    estimate_cost_ceiling,
    parse_text_blocks,
    post_messages,
)
from wobblebot.adapters.ollama import (
    OllamaJsonExtractError,
    extract_last_json_object,
)
from wobblebot.config.prompts import Prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.assistant import AssistantPort, ConversationContext
from wobblebot.ports.exceptions import AssistantError
from wobblebot.ports.operator import OperatorIntent
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cost_gate import (
    GateDeny,
    LLMCostConfig,
    SessionCostTracker,
    check_budget,
)
from wobblebot.services.llm_retry import LLMRetryConfig, retry_with_backoff

_LOGGER = logging.getLogger("wobblebot.adapters.anthropic_assistant")

# Match the sister advisor adapter's defaults. Re-declared locally so this
# module doesn't depend on private symbols in adapters/anthropic.py.
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_API_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Module-level TypeAdapter — Pydantic discriminator resolution is the only
# way to materialize the right OperatorIntent variant. Cheap to construct
# once.
_INTENT_ADAPTER: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)


class AnthropicAssistantAdapter(AssistantPort):  # pylint: disable=too-many-instance-attributes
    """Anthropic-backed ``AssistantPort`` for the operator interaction layer.

    Args:
        model: Anthropic model id (e.g. ``"claude-sonnet-4-6"``).
        prompt: Validated operator-role prompt
            (``config/prompts/operator.md``).
        api_key: Anthropic API key (from ``ANTHROPIC_API_KEY`` env).
        storage: Where to persist ``LLMCallRecord`` rows. The cost
            gate reads from the same store.
        session_tracker: In-memory running spend tally for the
            current CLI session.
        cost_config: Per-day + per-session USD caps.
        retry_config: max_retries + backoff knobs.
        base_url: Override for tests.
        api_version: ``anthropic-version`` header value.
        temperature: Sampling temperature (lower for deterministic
            intent parsing).
        max_tokens: Hard cap on response tokens. Intent payloads are
            small — 512 is plenty.
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
        api_key: str,
        storage: StoragePort,
        session_tracker: SessionCostTracker,
        cost_config: LLMCostConfig,
        retry_config: LLMRetryConfig,
        base_url: str = _DEFAULT_BASE_URL,
        api_version: str = _DEFAULT_API_VERSION,
        temperature: float = 0.3,
        max_tokens: int = 512,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicAssistantAdapter requires non-empty api_key")
        if prompt.metadata.role != "operator":
            raise AssistantError(
                f"AnthropicAssistantAdapter requires an operator-role prompt; "
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
        self._api_version = api_version
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Release the underlying httpx client if the adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def parse_intent(  # pylint: disable=too-many-locals
        self,
        context: ConversationContext,
    ) -> OperatorIntent:
        """Send the conversation context to Anthropic and return a typed intent.

        Raises:
            LLMCostCapExceeded: The cost gate denied the call.
            AssistantError: Transport failure, malformed envelope,
                JSON parse failure, or output that fails
                ``OperatorIntent`` schema validation.
        """
        system_prompt = self._build_system_prompt(context)
        messages = self._build_messages(context)

        # Cost-gate check before issuing the call. Estimate uses the
        # concatenated prompt text plus max_tokens ceiling per
        # ADR-014 decision 4. Anthropic system + messages are sent
        # separately on the wire but both count toward input tokens.
        prompt_text = system_prompt + "\n\n" + "\n".join(m["content"] for m in messages)
        estimate = estimate_cost_ceiling(
            model=self._model,
            prompt_text=prompt_text,
            max_tokens=self._max_tokens,
        )
        decision = await check_budget(
            self._storage,
            role="operator",
            estimated_cost_usd=estimate,
            session_spent_usd=self._session_tracker.total,
            config=self._cost_config,
        )
        if isinstance(decision, GateDeny):
            raise LLMCostCapExceeded(
                cap_kind=decision.cap_kind,
                cap_value_usd=decision.cap_value_usd,
                daily_spent_usd=decision.daily_spent_usd,
                session_spent_usd=decision.session_spent_usd,
                message=decision.reason,
            )

        body: dict[str, Any] = {
            "model": self._model,
            "system": system_prompt,
            "messages": messages,
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

        try:
            envelope = await retry_with_backoff(_call, self._retry_config)
        except httpx.HTTPStatusError as exc:
            await self._record_failure(exc)
            raise AssistantError(
                f"Anthropic request failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            await self._record_failure(exc)
            raise AssistantError(f"Anthropic transport error: {exc}") from exc

        # Success: persist the real cost first, then parse the answer.
        usage = envelope.get("usage", {}) or {}
        record = build_call_record(
            role="operator",
            model=self._model,
            usage=usage,
            request_id=envelope.get("id"),
            success=True,
            error_kind=None,
        )
        await self._storage.save_llm_call(record)
        self._session_tracker.add(record.cost_usd)

        inner = _extract_intent_dict(envelope)
        try:
            return _INTENT_ADAPTER.validate_python(inner)
        except ValidationError as exc:
            raise AssistantError(
                f"LLM output failed operator_intent_v1 schema validation: {exc}"
            ) from exc

    # ---- internals ------------------------------------------------ #

    def _build_system_prompt(self, context: ConversationContext) -> str:
        """System prompt = prompt body + engine state snapshot JSON.

        Same shape as the Ollama assistant adapter so the operator's
        prompt file remains provider-agnostic.
        """
        return (
            f"{self._prompt.body}\n\n"
            "Current engine state (JSON):\n"
            f"{context.engine_state_snapshot.model_dump_json(indent=2)}"
        )

    def _build_messages(self, context: ConversationContext) -> list[dict[str, str]]:
        """Compose the role-tagged message list for Anthropic's Messages API.

        Recent turns map operator → user and assistant → assistant.
        Current operator message is the final ``user`` turn.

        Anthropic requires alternating user/assistant turns; if a turn
        history violates that (e.g. two consecutive user turns from
        when the parse failed without a reply), the API rejects the
        request. We assume well-formed history from the cli/operator
        daemon; if it ever sends malformed history, the
        ``AssistantError`` from the resulting 400 surfaces cleanly to
        Discord.
        """
        messages: list[dict[str, str]] = []
        for turn in context.recent_turns:
            messages.append(
                {
                    "role": "user" if turn.role == "operator" else "assistant",
                    "content": turn.content,
                }
            )
        messages.append({"role": "user", "content": context.current_message})
        return messages

    async def _record_failure(self, exc: Exception) -> None:
        """Best-effort failure record. Swallows StorageError — losing one
        forensic row must not mask the original API failure to the caller."""
        error_kind = _classify_error(exc)
        record = LLMCallRecord(
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="operator",
            provider="anthropic",
            model=self._model,
            tokens_in=0,
            tokens_out=0,
            tokens_reasoning=None,
            cost_usd=Decimal("0"),
            request_id=None,
            success=False,
            error_kind=error_kind,
        )
        try:
            await self._storage.save_llm_call(record)
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.warning(
                "failed to persist failure record; original error will still raise",
                extra={"model": self._model, "error_kind": error_kind},
            )


def _classify_error(exc: Exception) -> str:
    """Short label for the LLMCallRecord.error_kind column."""
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


def _extract_intent_dict(envelope: dict[str, Any]) -> dict[str, Any]:
    """Pull the LLM's JSON object out of the Anthropic envelope.

    The model sometimes wraps the JSON in explanatory prose; the
    extract_last_json_object helper walks for the final ``{...}`` block.
    Falls back to a direct ``json.loads`` for bare-JSON responses.
    """
    raw_text = parse_text_blocks(envelope.get("content", []) or [])
    if not raw_text.strip():
        raise AssistantError(
            f"Anthropic response empty across content blocks; " f"envelope keys: {sorted(envelope)}"
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
                f"Anthropic response is JSON but not an object: {type(parsed).__name__}"
            ) from exc
        return parsed

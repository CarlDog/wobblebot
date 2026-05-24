"""OllamaAssistantAdapter — Stage 5.3 ``AssistantPort`` backed by Ollama.

Sister adapter to ``adapters/ollama.py`` (which implements
``AdvisorPort`` for the Phase 3 strategy advisor). Same Ollama
infrastructure, different port: this adapter takes a
``ConversationContext`` (current operator message + recent turn
history + engine state snapshot) and returns a typed ``OperatorIntent``
(``Command`` | ``Query`` | ``Conversational`` | ``Unparseable``).

**Endpoint:** ``/api/chat`` (not ``/api/generate`` like the advisor).
The chat endpoint accepts role-tagged messages (``system`` / ``user`` /
``assistant``), which gives the LLM a structured multi-turn history
instead of a concatenated prompt. Better behavior for context-sensitive
intent parsing ("now filter to ETH" referring to the prior turn's
``recent_fills`` query).

**Shared with the advisor adapter:** ``is_thinking_model`` and
``extract_last_json_object`` (promoted from underscore-private to
module-public in ``adapters/ollama.py``). Each adapter wraps the
extractor's ``OllamaJsonExtractError`` as its own port-specific error
(``AssistantError`` here, ``AdvisorError`` there).

**Output validation:** the LLM's JSON is validated against the
``OperatorIntent`` discriminated-union ``TypeAdapter``. Two levels of
discriminator (outer ``Command``/``Query``/``Conversational``/
``Unparseable``, inner concrete command/query kind) resolve in one
validation pass. Validation failure raises ``AssistantError`` so the
``cli/operator`` daemon (Stage 5.6) can post a graceful "I couldn't
parse that" reply.

**Per ADR-013:** this adapter is NOT in the money path. An
``AssistantError`` only affects the Discord chat surface; ``cli/live``
never imports this module.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from wobblebot.adapters.ollama import (
    OllamaJsonExtractError,
    extract_last_json_object,
    is_thinking_model,
)
from wobblebot.config.prompts import Prompt
from wobblebot.ports.assistant import AssistantPort, ConversationContext
from wobblebot.ports.exceptions import AssistantError
from wobblebot.ports.operator import OperatorIntent
from wobblebot.services.llm_cloud_call import INTENT_ADAPTER

_LOGGER = logging.getLogger(__name__)


class _OllamaEmptyContentRetry(Exception):
    """Marker raised by the envelope extractor when Ollama returns an
    empty ``message.content`` and no ``thinking`` field.

    Caught by ``parse_intent`` to trigger a single same-payload retry.
    Empirically (per docs/reference/operator-llm-models.md) qwen3.6
    returns empty content on 3/14 messages as a transient model hiccup;
    a single retry recovers most of these without surfacing the
    failure to the operator.
    """


_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Minimum max_tokens for thinking models. Reasoning models (deepseek-r1,
# phi4-reasoning, qwq, etc.) emit a chain-of-thought block before the
# JSON answer; the default 512 cap doesn't leave room for both. 4096
# is enough for ~3500 tokens of thinking + ~500 tokens of JSON output,
# which empirically covers every Ollama-served reasoning model we've
# tested (see docs/reference/operator-llm-models.md). Operators can
# still set ``assistant.max_tokens`` higher in settings.yml for very
# verbose reasoning models; the floor only RAISES the configured
# value, never lowers it.
_THINKING_MODEL_MIN_TOKENS = 4096

# Models that cannot produce schema-conforming OperatorIntent JSON.
# Matched case-insensitively against the model tag. Determined empirically
# via tools/probe_assistant.py + tmp_model_compare.py on 2026-05-24:
#
# - ``phi4-mini-reasoning:3.8b-fp16`` -- math-reasoning specialist; 0/14
#   on the routing battery. Treats every prompt as a math problem
#   ("calculate x in a quadratic equation" / "compute profit from 0.5
#   BTC at $40k") and never emits any JSON. 3.8B params + math
#   specialization is fundamentally wrong-fit for instruction-following
#   intent parsing. No prompt edit can salvage this.
# - ``llava`` -- vision model, not text-instruction-tuned.
#
# These models stay INSTALLED in Ollama for potential use by other
# wobblebot roles (e.g. a future MoE quant-expert that does numerical
# analysis). This list only gates the operator-assistant role.
KNOWN_INCOMPATIBLE_FOR_ASSISTANT = (
    "phi4-mini-reasoning",
    "llava",
)

# Models that work but with observed failure modes (degraded). Operator
# is warned but the daemon still starts. Empirically determined as above:
#
# - ``qwen3.6:35b-a3b-q8_0`` -- 11/14 on the routing battery with 3
#   silent "empty message.content and no thinking field" returns.
#   Likely transient model hiccups; operator should expect occasional
#   "Sorry, I couldn't process that" replies.
KNOWN_DEGRADED_FOR_ASSISTANT = ("qwen3.6:35b-a3b",)


def check_model_suitability(model: str) -> None:
    """Refuse known-incompatible models; warn on known-degraded.

    Called at adapter ``__init__``. Hard-fails with ``AssistantError``
    when the configured model is on the incompatible list; logs a
    WARNING when it's on the degraded list. Anything else passes
    silently.

    Pattern match is substring case-insensitive against the model
    tag, so e.g. ``phi4-mini-reasoning:3.8b-fp16`` matches the
    ``phi4-mini-reasoning`` entry.
    """
    name = model.lower()
    for pattern in KNOWN_INCOMPATIBLE_FOR_ASSISTANT:
        if pattern in name:
            raise AssistantError(
                f"Model {model!r} is known-incompatible with the "
                f"operator-assistant role (pattern matched: {pattern!r}). "
                f"Pick a general instruct-tuned model -- recommendations: "
                f"phi4:14b-q8_0, mistral-nemo:12b-instruct-2407-q8_0, "
                f"phi4-reasoning:14b-plus-q8_0, granite4.1:30b-q5_K_M. "
                f"See docs/reference/operator-llm-models.md for the full "
                f"compatibility matrix."
            )
    for pattern in KNOWN_DEGRADED_FOR_ASSISTANT:
        if pattern in name:
            _LOGGER.warning(
                "model %r is known-degraded for the operator-assistant role "
                "(pattern: %r). Expect occasional parse failures. See "
                "docs/reference/operator-llm-models.md for alternatives.",
                model,
                pattern,
            )


class OllamaAssistantAdapter(AssistantPort):  # pylint: disable=too-many-instance-attributes
    """Ollama-backed ``AssistantPort`` for the operator interaction layer.

    Args:
        model: Ollama model tag (e.g. ``"phi4:14b"``).
        prompt: Validated operator prompt (loaded from
            ``config/prompts/operator.md``). The body is sent as the
            ``system`` message; the engine state snapshot is appended
            to it so the LLM sees current state on every turn.
        base_url: Ollama server URL.
        temperature: Sampling temperature (lower for deterministic
            intent parsing; defaults to 0.3 per the prompt's
            ``temperature_hint``).
        max_tokens: ``num_predict`` cap on response length. Operator
            intent payloads are small; 512 is plenty.
        timeout_seconds: HTTP timeout for the chat call.
        client: Optional pre-constructed ``httpx.AsyncClient`` (test
            seam). If ``None``, the adapter creates and owns one;
            ``aclose()`` releases it.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        model: str,
        prompt: Prompt,
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.3,
        max_tokens: int = 512,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if prompt.metadata.role != "operator":
            raise AssistantError(
                f"OllamaAssistantAdapter requires an operator-role prompt; "
                f"got role={prompt.metadata.role!r}"
            )
        check_model_suitability(model)
        self._model = model
        self._prompt = prompt
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        # Raise max_tokens to the thinking-model floor if needed. Lower
        # configured values for reasoning models result in truncated
        # thinking blocks with no JSON answer at all.
        if is_thinking_model(model) and max_tokens < _THINKING_MODEL_MIN_TOKENS:
            _LOGGER.info(
                "raising max_tokens from %d to %d for thinking model %r "
                "(see docs/reference/operator-llm-models.md)",
                max_tokens,
                _THINKING_MODEL_MIN_TOKENS,
                model,
            )
            self._max_tokens = _THINKING_MODEL_MIN_TOKENS
        else:
            self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Release the underlying httpx client if the adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def parse_intent(self, context: ConversationContext) -> OperatorIntent:
        """Send the conversation context to Ollama and return a typed intent.

        Retries once on transient empty-content responses (see the
        ``_OllamaEmptyContentRetry`` marker). Schema-validation failures
        do NOT retry -- they're content issues, not transport hiccups.

        Raises:
            AssistantError: Transport failure, malformed envelope,
                JSON parse failure, or output that fails
                ``OperatorIntent`` schema validation. Empty-content
                failures raise only after the retry also returns empty.
        """
        messages = self._build_messages(context)
        thinking_mode = is_thinking_model(self._model)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            },
        }
        if not thinking_mode:
            payload["format"] = "json"

        inner = await self._request_with_retry(payload, thinking_mode=thinking_mode)

        try:
            return INTENT_ADAPTER.validate_python(inner)
        except ValidationError as exc:
            raise AssistantError(
                f"LLM output failed operator_intent_v1 schema validation: {exc}"
            ) from exc

    async def _request_with_retry(
        self, payload: dict[str, Any], *, thinking_mode: bool
    ) -> dict[str, Any]:
        """POST + extract the inner dict, retrying once on empty content."""
        try:
            return await self._post_and_extract(payload, thinking_mode=thinking_mode)
        except _OllamaEmptyContentRetry as exc:
            _LOGGER.warning(
                "Ollama returned empty content for model %r; retrying once",
                self._model,
            )
            try:
                return await self._post_and_extract(payload, thinking_mode=thinking_mode)
            except _OllamaEmptyContentRetry as retry_exc:
                # Both attempts empty -- surface as the original
                # AssistantError shape so callers don't need to know
                # about the marker.
                raise AssistantError(str(retry_exc)) from exc

    async def _post_and_extract(
        self, payload: dict[str, Any], *, thinking_mode: bool
    ) -> dict[str, Any]:
        """One POST + envelope extraction; transport errors wrap as AssistantError."""
        try:
            response = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            response.raise_for_status()
            envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise AssistantError(f"Ollama chat request failed: {exc}") from exc
        return self._extract_intent_dict(envelope, thinking_mode=thinking_mode)

    async def summarize(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 2048
    ) -> str:
        """One-shot Ollama ``/api/chat`` call returning plain text.

        Used by ``StatusReportQuery`` to condense structured query
        results into prose. Unlike :meth:`parse_intent` this does NOT
        request JSON-formatted output — the LLM is free to write
        Markdown / paragraphs.

        Raises:
            AssistantError: Transport failure or malformed envelope.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": max_tokens,
            },
        }
        try:
            response = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            response.raise_for_status()
            envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise AssistantError(f"Ollama summarize request failed: {exc}") from exc

        message = envelope.get("message")
        if not isinstance(message, dict):
            raise AssistantError(
                f"Ollama chat envelope missing 'message' object; keys: {sorted(envelope)}"
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise AssistantError("Ollama chat envelope 'message.content' is not a string")
        return content.strip()

    # ---- internals ------------------------------------------------ #

    def _build_messages(self, context: ConversationContext) -> list[dict[str, str]]:
        """Compose the role-tagged message list for Ollama's chat API.

        System message = prompt body + the engine state snapshot (JSON).
        Then each recent turn becomes a user / assistant message in
        chronological order. Finally the current operator message is
        appended as the last ``user`` turn.
        """
        system = (
            f"{self._prompt.body}\n\n"
            "Current engine state (JSON):\n"
            f"{context.engine_state_snapshot.model_dump_json(indent=2)}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for turn in context.recent_turns:
            messages.append(
                {
                    "role": "user" if turn.role == "operator" else "assistant",
                    "content": turn.content,
                }
            )
        messages.append({"role": "user", "content": context.current_message})
        return messages

    def _extract_intent_dict(
        self,
        envelope: dict[str, Any],
        *,
        thinking_mode: bool,
    ) -> dict[str, Any]:
        """Pull the LLM's JSON object out of the chat envelope.

        ``/api/chat`` returns ``{"message": {"role": "assistant",
        "content": "..."}, ...}``. Some newer Ollama versions also
        surface a top-level ``"thinking"`` field for thinking models;
        we treat both as one combined text and walk for the last
        balanced JSON object when in thinking mode.
        """
        message = envelope.get("message")
        if not isinstance(message, dict):
            raise AssistantError(
                f"Ollama chat envelope missing 'message' object; keys: {sorted(envelope)}"
            )
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        raw_thinking_field = envelope.get("thinking")
        if not isinstance(raw_thinking_field, str):
            raw_thinking_field = ""
        content_empty = not content.strip()
        thinking_present = bool(raw_thinking_field.strip())

        if content_empty and not thinking_present:
            raise _OllamaEmptyContentRetry(
                "Ollama chat returned empty 'message.content' and no 'thinking' field"
            )

        if thinking_mode or content_empty:
            combined = content
            if thinking_present:
                combined = (combined + "\n" + raw_thinking_field).strip()
            try:
                return extract_last_json_object(combined)
            except OllamaJsonExtractError as exc:
                raise AssistantError(str(exc)) from exc

        try:
            parsed: Any = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AssistantError(
                f"Ollama 'message.content' is not valid JSON despite format=json request: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise AssistantError(
                f"Ollama 'message.content' decoded to {type(parsed).__name__}, expected object"
            )
        return parsed

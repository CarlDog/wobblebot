"""Authenticated Ollama Cloud advisor; distinct from the unbilled local adapter.

Uses the native non-streaming /api/chat endpoint. Cloud does not support
structured outputs, so the existing prompt and local recommendation validator
provide the JSON contract. No local host override or automatic model aliasing.
"""

from __future__ import annotations

from typing import Any

import httpx

from wobblebot.config.prompts import Prompt
from wobblebot.domain.llm_cost import LLMRole
from wobblebot.ports.advisor import AdvisorPort, AdvisorRecommendation, PerformanceSummary
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

_CHAT_URL = "https://ollama.com/api/chat"


def _token_count(envelope: dict[str, Any], field: str, default: int | None = None) -> int:
    value = envelope.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdvisorError("Ollama Cloud response has missing or invalid token usage")
    return value


def extract_ollama_cloud_tokens(envelope: dict[str, Any]) -> TokenUsage:
    """Normalize native prompt/cache counts; generated tokens include thinking.

    https://docs.ollama.com/api/chat (verified 2026-09-08). Missing cache
    counts conservatively bill full input. Missing/invalid total usage is an
    error, never a free call. No separate reasoning count is reported.
    """
    prompt = _token_count(envelope, "prompt_eval_count")
    output = _token_count(envelope, "eval_count")
    cached = _token_count(envelope, "prompt_eval_cached_count", 0)
    if cached > prompt:
        raise AdvisorError("Ollama Cloud cached token count exceeds prompt count")
    return TokenUsage(tokens_in=prompt - cached, tokens_out=output, tokens_cache_read=cached)


class OllamaCloudAdvisorAdapter(AdvisorPort):
    """Cloud advisor with the shared retry policy, spend gate and forensic ledger."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
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
        temperature: float = 0.5,
        max_tokens: int = 4000,
        timeout_seconds: float = 300,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OllamaCloudAdvisorAdapter requires non-empty api_key")
        self._ctx = CloudCallContext(
            storage=storage,
            session_tracker=session_tracker,
            cost_config=cost_config,
            retry_config=retry_config,
            role=role,
            provider="ollama_cloud",
            model=model,
        )
        self._prompt = prompt
        self._api_key = api_key
        self._options = {"temperature": temperature, "num_predict": max_tokens}
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            _CHAT_URL,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            follow_redirects=False,
        )
        response.raise_for_status()
        try:
            envelope = response.json()
        except (ValueError, UnicodeError) as exc:
            raise AdvisorError("Ollama Cloud returned a non-JSON envelope") from exc
        if not isinstance(envelope, dict) or "error" in envelope:
            raise AdvisorError("Ollama Cloud returned an invalid response envelope")
        # Validate accounting inside call_fn so an invalid usage envelope gets a
        # failed ledger row. Never treat absent token counts as zero cost.
        extract_ollama_cloud_tokens(envelope)
        return envelope

    async def get_recommendation(
        self, summary: PerformanceSummary, *, extra_context: str = ""
    ) -> AdvisorRecommendation:
        user_message = (
            "Current engine state (JSON):\n\n"
            f"{summary.model_dump_json(indent=2)}\n\n"
            "Respond with JSON conforming to advisor_recommendation_v1."
        )
        if extra_context:
            user_message += f"\n\n{extra_context}"
        body = {
            "model": self._ctx.model,
            "messages": [
                {"role": "system", "content": self._prompt.body},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": self._options,
        }

        async def call() -> dict[str, Any]:
            return await self._post(body)

        async with wrap_provider_errors("Ollama Cloud", AdvisorError):
            estimate = estimate_cost_ceiling(
                provider="ollama_cloud",
                model=self._ctx.model,
                prompt_text=f"{self._prompt.body}\n\n{user_message}",
                max_tokens=self._max_tokens,
            )
            envelope = await execute_cloud_call(
                ctx=self._ctx,
                estimated_cost_usd=estimate,
                call_fn=call,
                extract_tokens=extract_ollama_cloud_tokens,
            )
        # Account for generated tokens even when the answer is truncated or invalid.
        message = envelope.get("message")
        if envelope.get("done") is not True or envelope.get("done_reason") == "length":
            raise AdvisorError("Ollama Cloud response incomplete; check output-token cap")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise AdvisorError("Ollama Cloud response has no assistant text")
        return parse_advisor_recommendation(
            message["content"], fallback_role=self._ctx.role, provider_name="Ollama Cloud"
        )

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True

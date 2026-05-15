"""OllamaAdapter — Stage 3.2 ``AdvisorPort`` implementation backed by a local Ollama server.

Single-LLM advisor: one model, one prompt file, one round-trip per
``get_recommendation`` call. The MoE adapter (Stage 3.4a) will compose
multiple per-provider expert adapters; this one is the simplest case
and serves as the baseline.

**Wire format.** The adapter POSTs to Ollama's ``/api/generate``
endpoint with ``format: "json"`` so the response body is a JSON
object string. The LLM is expected to emit the
``advisor_recommendation_v1`` schema declared in the prompt file's
frontmatter (see ``config/prompts/quant.md``): ``{ role,
recommendations, rationale, confidence }``. ``recommendation_id``
and ``timestamp`` are populated by this adapter, not the LLM.

**Error wrapping.** Transport, HTTP status, JSON-parse, and
Pydantic-validation failures are wrapped as ``AdvisorError`` with
the original exception chained (``raise ... from exc``). Callers
depend on the port's contract, not on httpx or json semantics.

**Client lifecycle.** Pass ``client=httpx.AsyncClient(transport=...)``
for tests (MockTransport pattern). If no client is supplied the
adapter constructs and owns one; call ``aclose()`` to release it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from wobblebot.config.prompts import Prompt
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_SECONDS = 60.0


class OllamaAdapter(AdvisorPort):  # pylint: disable=too-many-instance-attributes
    """Ollama-backed single-LLM ``AdvisorPort`` implementation.

    Args:
        model: Ollama model tag (e.g. ``"deepseek-r1:7b"``).
        prompt: Validated prompt file (frontmatter + body). The
            body becomes the system prompt; the summary is appended
            as JSON.
        role: Value to use for ``AdvisorRecommendation.role`` if the
            LLM omits the field. Defaults to ``"single"`` to match
            the Stage 3.2 invocation pattern.
        base_url: Ollama server URL. Defaults to localhost:11434.
        temperature: Sampling temperature (0.0–2.0).
        max_tokens: ``num_predict`` cap on response length.
        timeout_seconds: HTTP timeout for the generate call.
        client: Optional pre-constructed ``httpx.AsyncClient`` (test
            seam). If ``None``, the adapter creates its own and
            ``aclose()`` releases it.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        model: str,
        prompt: Prompt,
        role: str = "single",
        base_url: str = _DEFAULT_BASE_URL,
        temperature: float = 0.5,
        max_tokens: int = 512,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._prompt = prompt
        self._role = role
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Release the underlying httpx client if the adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        user_message = (
            "Current engine state (JSON):\n\n"
            f"{summary.model_dump_json(indent=2)}\n\n"
            "Respond with JSON conforming to advisor_recommendation_v1."
        )
        payload = {
            "model": self._model,
            "prompt": f"{self._prompt.body}\n\n{user_message}",
            "format": "json",
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            },
        }
        try:
            response = await self._client.post(f"{self._base_url}/api/generate", json=payload)
            response.raise_for_status()
            ollama_envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise AdvisorError(f"Ollama request failed: {exc}") from exc

        raw_response = ollama_envelope.get("response")
        if not isinstance(raw_response, str) or not raw_response.strip():
            raise AdvisorError(
                "Ollama response missing or empty 'response' field; "
                f"envelope keys: {sorted(ollama_envelope)}"
            )

        try:
            inner: dict[str, Any] = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise AdvisorError(
                f"Ollama 'response' is not valid JSON despite format=json request: {exc}"
            ) from exc

        try:
            return AdvisorRecommendation(
                recommendation_id=str(uuid4()),
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role=str(inner.get("role", self._role)),
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

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        """Stage 3.2: parsing-success is the only check.

        Real safety-bound enforcement (whitelist of mutable config
        keys, magnitude caps) is the auto-apply gate's job in Stage
        3.4b. At this layer we trust that a recommendation that
        survived ``AdvisorRecommendation`` construction is
        structurally well-formed.
        """
        del recommendation
        return True

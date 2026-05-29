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

# Substrings (matched case-insensitively against the model tag) that
# identify "thinking" models — those that emit chain-of-thought
# reasoning before the answer. Ollama's ``format: "json"`` constraint
# forces the very first emitted token to start a valid JSON value, so
# these models degenerate to ``{}`` under that mode. We drop the
# constraint and pull the final JSON object out of the free-text body
# instead. ``qwq`` is reasoning-tuned but emits JSON directly under
# format=json — it does NOT need this path and is deliberately not
# listed here.
_THINKING_MODEL_PATTERNS = (
    "deepseek-r1",
    ":r1",
    "o1-",
    "thinker",
    "thinking",
    "reasoning",
)


def is_thinking_model(model_tag: str) -> bool:
    """Return True iff the Ollama model tag matches a known thinking-style model.

    Used by the advisor adapter (this module) and the assistant adapter
    (``adapters/ollama_assistant.py``) to decide whether to drop Ollama's
    ``format: "json"`` constraint and walk the free-text body for the
    final JSON object instead.
    """
    name = model_tag.lower()
    return any(pattern in name for pattern in _THINKING_MODEL_PATTERNS)


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
        raise OllamaJsonExtractError(
            f"Thinking-mode model returned no parseable JSON object "
            f"in {len(text)} chars of output"
        )
    return candidates[-1]


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
        force_json: bool = False,
    ) -> None:
        self._model = model
        self._prompt = prompt
        self._role = role
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        # Diagnostic escape hatch (2026-05-25): when True, force
        # ``format=json`` even for thinking-model name patterns. The
        # default-False preserves existing production behavior. Used by
        # ``tools/probe_advisor.py --force-json`` to evaluate whether
        # newer reasoning-tuned models actually need the free-text
        # extraction path. See ``docs/release/v1.1/operator-ux.md`` →
        # "Reasoning-model support" for the planned config wiring.
        self._force_json = force_json

    async def aclose(self) -> None:
        """Release the underlying httpx client if the adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def get_recommendation(  # pylint: disable=too-many-locals
        self,
        summary: PerformanceSummary,
        *,
        extra_context: str = "",
    ) -> AdvisorRecommendation:
        # ``extra_context`` is a Stage 3.4a-specific channel for the MoE
        # arbitrator: when an arbitrator-role expert is invoked, the
        # other experts' opinions are serialized into this string so the
        # arbitrating LLM can synthesize a final call from them. Default
        # empty preserves AdvisorPort-compatible behavior for the
        # single-LLM path.
        user_message = (
            "Current engine state (JSON):\n\n"
            f"{summary.model_dump_json(indent=2)}\n\n"
            "Respond with JSON conforming to advisor_recommendation_v1."
        )
        if extra_context:
            user_message = f"{user_message}\n\n{extra_context}"
        # When force_json overrides the heuristic, downstream parsing must
        # ALSO treat the response as direct-JSON (format=json suppresses
        # the <think> block, so the free-text extraction path is wrong).
        thinking_mode = is_thinking_model(self._model) and not self._force_json
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": f"{self._prompt.body}\n\n{user_message}",
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            },
        }
        # Non-thinking models honor Ollama's `format: "json"` constraint,
        # which forces the response body to be a parseable JSON value.
        # Thinking models (R1, o1-style) emit a reasoning preamble first,
        # so we drop the constraint and extract the trailing JSON from
        # free text instead — see ``extract_last_json_object``. The
        # 2026-05-25 diagnostic showed newer reasoning models (phi4-
        # reasoning) actually emit clean JSON under format=json, so the
        # probe ``--force-json`` flag bypasses this heuristic.
        if self._force_json or not thinking_mode:
            payload["format"] = "json"

        try:
            response = await self._client.post(f"{self._base_url}/api/generate", json=payload)
            response.raise_for_status()
            ollama_envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            # Include the exception type: a bare ReadTimeout/ConnectTimeout
            # often has an empty str(), which left this message as a
            # useless "Ollama request failed: " (the 2026-05-28 NAS
            # advise-timeout incident needed Ollama's own GIN log to
            # diagnose). The type name disambiguates timeout vs transport.
            raise AdvisorError(f"Ollama request failed: {type(exc).__name__}: {exc}") from exc

        raw_response_field = ollama_envelope.get("response")
        raw_thinking_field = ollama_envelope.get("thinking")
        raw_response = raw_response_field if isinstance(raw_response_field, str) else ""
        raw_thinking = raw_thinking_field if isinstance(raw_thinking_field, str) else ""
        response_empty = not raw_response.strip()
        thinking_present = bool(raw_thinking.strip())

        if response_empty and not thinking_present:
            raise AdvisorError(
                "Ollama response empty across both 'response' and 'thinking' fields; "
                f"envelope keys: {sorted(ollama_envelope)}"
            )

        inner: dict[str, Any]
        # Two routes into the extractor:
        # 1. thinking_mode is set by name pattern (R1, o1, "thinking", etc.) —
        #    the model emits CoT + final JSON in one stream, free-text extract.
        # 2. response_empty + thinking_present — newer Ollama versions split
        #    the model's output into separate `thinking` and `response` fields.
        #    Some models (qwen3, nemotron3) emit the actual answer into
        #    `thinking` even when format=json is requested. Treat the
        #    combined text as a thinking-mode response and extract.
        if thinking_mode or response_empty:
            combined = raw_response
            if thinking_present:
                joined = (combined + "\n" + raw_thinking).strip()
                combined = joined
            try:
                inner = extract_last_json_object(combined)
            except OllamaJsonExtractError as exc:
                raise AdvisorError(str(exc)) from exc
        else:
            try:
                inner = json.loads(raw_response)
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

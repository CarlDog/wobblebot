"""OllamaAdapter — Stage 3.2 ``AdvisorPort`` implementation backed by a local Ollama server.

Single-LLM advisor: one model, one prompt file, one round-trip per
``get_recommendation`` call. The MoE adapter (Stage 3.4a) will compose
multiple per-provider expert adapters; this one is the simplest case
and serves as the baseline.

**Wire format.** The adapter POSTs to Ollama's ``/api/generate``
endpoint with a **JSON Schema** in ``format`` (see
``_RESPONSE_JSON_SCHEMA``), so the server constrains generation to the
``advisor_recommendation_v1`` shape declared in the prompt file's
frontmatter (see ``config/prompts/quant.md``): ``{ role,
recommendations, rationale, confidence }``. ``recommendation_id``
and ``timestamp`` are populated by this adapter, not the LLM.

Previously this sent the bare string ``format: "json"``, which
guarantees the body *parses* but says nothing about its *shape* — and
that gap was the failure mode, not a theoretical one. A 2026-08-10 MoE
run had two of four models return perfectly valid JSON with invented
keys (``{bollinger_middle, recommend}`` from a quant expert;
``{"**Recommendation", "Rationale"}`` — markdown headings as keys —
from the arbitrator), each surfacing as a post-hoc
``missing required field 'confidence'``. Ollama has supported
schema-constrained generation since 0.5; passing the real schema moves
the contract from something we validate after the fact to something
the server cannot violate. (Per the standing rule: prefer the
upstream's own validation over a local copy of it.)

The schema is deliberately built from the **well-supported** JSON
Schema subset — ``type`` / ``properties`` / ``required`` / ``enum``
only. Ollama converts the schema to a GBNF grammar via llama.cpp,
whose converter handles a subset of the spec; keywords like
``minLength`` are omitted here and left to Pydantic on the parse side,
so a grammar-conversion failure can't take out the whole call path.

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
from typing import Any, get_args
from uuid import uuid4

import httpx
from pydantic import ValidationError

from wobblebot.config.prompts import Prompt
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    ConfidenceLevel,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_SECONDS = 60.0

# The wire contract, as a JSON Schema Ollama can enforce during
# generation. This is deliberately NOT ``AdvisorRecommendation.
# model_json_schema()``: that model also carries ``recommendation_id``,
# ``timestamp``, ``expert_opinions`` and ``news_materially_drove``,
# which this adapter assigns and the LLM must never supply. What the
# model owes us is the strict subset below.
#
# ``required`` mirrors what the parser actually cannot recover from:
# ``confidence`` (KeyError) and ``rationale`` (``min_length=1`` on the
# domain model, so an omission fails validation anyway).
# ``recommendations`` is required as a KEY but may be ``{}`` — that is
# how a genuine "hold" is expressed, and forcing the key stops a model
# from signalling hold by silently omitting it (the probe battery
# scores an omitted spacing as a MISS for the same reason).
# ``role`` stays optional: the adapter defaults it to its own role, and
# a model echoing the wrong role shouldn't fail the call.
#
# The confidence enum is derived from ``ConfidenceLevel`` rather than
# retyped, so the grammar and the Pydantic literal cannot drift apart.
_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "recommendations": {"type": "object"},
        "rationale": {"type": "string"},
        "confidence": {"type": "string", "enum": list(get_args(ConfidenceLevel))},
    },
    "required": ["recommendations", "rationale", "confidence"],
}

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
        # Non-thinking models honor Ollama's `format` constraint, which
        # here is the full JSON Schema — so the server won't emit a body
        # that violates the wire contract in the first place. Thinking
        # models (R1, o1-style) emit a reasoning preamble first, so we
        # drop the constraint and extract the trailing JSON from free
        # text instead — see ``extract_last_json_object``. The
        # 2026-05-25 diagnostic showed newer reasoning models (phi4-
        # reasoning) actually emit clean JSON under a format constraint,
        # so the probe ``--force-json`` flag bypasses this heuristic.
        #
        # The gate is unchanged from when this sent the bare string
        # "json" — schema enforcement applies exactly where the weaker
        # constraint already did, so the only behavioural delta is that
        # the constrained path is now constrained to the RIGHT shape.
        if self._force_json or not thinking_mode:
            payload["format"] = _RESPONSE_JSON_SCHEMA

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
            # thinking, then response — extract_last_json_object takes the
            # LAST parseable candidate, and reasoning text routinely echoes
            # draft/example JSON (fleet-review #19 finding 5). Putting
            # `response` last means the model's actual final answer wins
            # over anything quoted earlier in its chain-of-thought.
            combined = raw_response
            if thinking_present:
                joined = (raw_thinking + "\n" + combined).strip()
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

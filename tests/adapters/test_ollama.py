"""Unit tests for OllamaAdapter (Stage 3.2 single-LLM advisor).

The HTTP layer is mocked via ``httpx.MockTransport`` so tests stay
deterministic and never touch a real Ollama server. Each test
controls exactly what the (mocked) Ollama returns.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wobblebot.adapters.ollama import (
    OllamaAdapter,
    _extract_last_json_object,
    _is_thinking_model,
)
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.ports.advisor import AdvisorRecommendation, PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError

pytestmark = pytest.mark.unit


def _make_prompt() -> Prompt:
    return Prompt(
        metadata=PromptMetadata(
            role="quant",
            description="Test prompt",
            response_schema="advisor_recommendation_v1",
            temperature_hint=0.5,
        ),
        body="You are a test quant expert. Emit JSON.",
        source_path=Path("config/prompts/quant.md"),
    )


def _make_summary() -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=24.0,
        latest_price=80000.0,
        snapshot_count=1000,
        volatility=0.0004,
        max_drawdown=-0.03,
        flatness=0.97,
        cycle_count=0,
        win_rate=0.0,
        total_pnl=0.0,
    )


def _ollama_response(inner_json: dict[str, object] | str) -> dict[str, object]:
    """Wrap an LLM output in Ollama's response envelope shape."""
    if isinstance(inner_json, dict):
        inner = json.dumps(inner_json)
    else:
        inner = inner_json
    return {
        "model": "test-model",
        "created_at": "2026-05-15T12:00:00Z",
        "response": inner,
        "done": True,
    }


def _build_adapter(
    transport: httpx.MockTransport,
    *,
    role: str = "single",
) -> OllamaAdapter:
    client = httpx.AsyncClient(transport=transport)
    return OllamaAdapter(
        model="test-model",
        prompt=_make_prompt(),
        role=role,
        client=client,
    )


@pytest.mark.asyncio
class TestGetRecommendationHappyPath:
    async def test_returns_validated_recommendation(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {"spacing_percentage": 1.2},
                        "rationale": "Volatility narrow; widen grid slightly.",
                        "confidence": "medium",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            rec = await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

        assert isinstance(rec, AdvisorRecommendation)
        assert rec.role == "quant"
        assert rec.confidence == "medium"
        assert rec.recommendations == {"spacing_percentage": 1.2}
        assert rec.rationale.startswith("Volatility narrow")
        # Adapter-populated fields
        assert rec.recommendation_id  # non-empty UUID string
        assert rec.timestamp.dt.tzinfo is not None
        # Request shape
        assert captured["url"] == "http://localhost:11434/api/generate"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "test-model"
        assert body["format"] == "json"
        assert body["stream"] is False
        assert "advisor_recommendation_v1" in body["prompt"]
        assert "BTC/USD" in body["prompt"]
        assert body["options"]["temperature"] == 0.5

    async def test_role_defaults_when_llm_omits_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        # No "role" field
                        "recommendations": {},
                        "rationale": "No change.",
                        "confidence": "low",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler), role="single")
        try:
            rec = await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()
        assert rec.role == "single"

    async def test_empty_recommendations_dict_allowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {},
                        "rationale": "Hold steady.",
                        "confidence": "high",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            rec = await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()
        assert rec.recommendations == {}

    async def test_recommendations_null_treated_as_empty(self) -> None:
        # LLMs sometimes emit `"recommendations": null` when they mean
        # "no change". Adapter normalizes to {} so callers always get a dict.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": None,
                        "rationale": "No change suggested.",
                        "confidence": "low",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            rec = await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()
        assert rec.recommendations == {}


@pytest.mark.asyncio
class TestGetRecommendationErrorPaths:
    async def test_http_500_wraps_as_advisor_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "model exploded"})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="Ollama request failed"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_connection_error_wraps_as_advisor_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="Ollama request failed"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_missing_response_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"done": True})  # no 'response'

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="missing or empty 'response' field"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_response_field_not_valid_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ollama_response("this is not json {{{"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="not valid JSON"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_missing_confidence_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {},
                        "rationale": "Hold.",
                        # Missing confidence
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="missing required field"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_invalid_confidence_value(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {},
                        "rationale": "Strong belief.",
                        "confidence": "very-high",  # not in Literal
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="schema validation"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_empty_rationale_fails_schema(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {},
                        "rationale": "",  # empty -> min_length=1 violation
                        "confidence": "high",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="schema validation"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestValidateRecommendation:
    async def test_returns_true_for_parsed_recommendation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {"spacing_percentage": 1.0},
                        "rationale": "OK.",
                        "confidence": "medium",
                    }
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            rec = await adapter.get_recommendation(_make_summary())
            assert await adapter.validate_recommendation(rec) is True
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestClientLifecycle:
    async def test_owns_client_when_none_passed(self) -> None:
        adapter = OllamaAdapter(model="m", prompt=_make_prompt())
        try:
            assert adapter._owns_client is True  # noqa: SLF001  pylint: disable=protected-access
        finally:
            await adapter.aclose()

    async def test_does_not_close_externally_owned_client(self) -> None:
        external_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        adapter = OllamaAdapter(model="m", prompt=_make_prompt(), client=external_client)
        await adapter.aclose()
        # External client should still be usable.
        assert not external_client.is_closed
        await external_client.aclose()

    async def test_base_url_trailing_slash_stripped(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "single",
                        "recommendations": {},
                        "rationale": "ok",
                        "confidence": "low",
                    }
                ),
            )

        adapter = OllamaAdapter(
            model="m",
            prompt=_make_prompt(),
            base_url="http://example:11434/",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()
        assert captured["url"] == "http://example:11434/api/generate"


class TestIsThinkingModel:
    """Pure helper — matches model tags that emit chain-of-thought."""

    @pytest.mark.parametrize(
        "name",
        [
            "deepseek-r1:7b",
            "deepseek-r1:14b",
            "deepseek-r1:32b",
            "DeepSeek-R1:14B",  # case-insensitive
            "o1-mini",
            "o1-preview",
            "qwen3-thinking:14b",
            "openthinker:7b",
            "some-other-reasoning-model:8b",
            "custom:r1-distill",
        ],
    )
    def test_matches_known_thinking_patterns(self, name: str) -> None:
        assert _is_thinking_model(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "phi4:14b",
            "mistral-nemo:12b",
            "gemma3:27b",
            "llama3.3:70b",
            "qwq:32b",  # reasoning-tuned but works with format=json
            "deepseek-coder-v2:16b",
            "nous-hermes2-mixtral:latest",
        ],
    )
    def test_does_not_match_non_thinking_models(self, name: str) -> None:
        assert _is_thinking_model(name) is False


class TestExtractLastJsonObject:
    """Pure helper — pulls a trailing JSON object out of free-text output."""

    def test_pure_json_returned(self) -> None:
        assert _extract_last_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_thinking_preamble_then_json(self) -> None:
        text = (
            "<think>\nLet me consider the metrics...\nVolatility is low.\n</think>\n\n"
            '{"role": "quant", "confidence": "medium"}'
        )
        assert _extract_last_json_object(text) == {"role": "quant", "confidence": "medium"}

    def test_json_in_code_fence(self) -> None:
        text = "```json\n" + '{"answer": 42}' + "\n```"
        assert _extract_last_json_object(text) == {"answer": 42}

    def test_multiple_objects_returns_last(self) -> None:
        # Thinking sometimes contains illustrative JSON-shaped examples
        # earlier in the reasoning; the last successful parse is the answer.
        text = 'Maybe try {"x": 1}. Or perhaps {"x": 2}. ' 'Final: {"x": 3, "confidence": "high"}'
        result = _extract_last_json_object(text)
        assert result == {"x": 3, "confidence": "high"}

    def test_braces_inside_strings_dont_break_parsing(self) -> None:
        # raw_decode is JSON-aware; a brace inside a string literal isn't
        # treated as an object boundary.
        text = '{"note": "the {value} is {nested}", "result": "ok"}'
        assert _extract_last_json_object(text) == {
            "note": "the {value} is {nested}",
            "result": "ok",
        }

    def test_no_json_raises_advisor_error(self) -> None:
        with pytest.raises(AdvisorError, match="no parseable JSON object"):
            _extract_last_json_object("I am unable to comply at this time.")

    def test_invalid_json_braces_are_skipped(self) -> None:
        # First `{` opens malformed JSON; second `{` opens valid.
        text = '{not valid json} but then {"ok": true}'
        assert _extract_last_json_object(text) == {"ok": True}

    def test_top_level_array_ignored(self) -> None:
        # Only object-typed JSON counts as a candidate.
        with pytest.raises(AdvisorError, match="no parseable JSON object"):
            _extract_last_json_object("[1, 2, 3]")


@pytest.mark.asyncio
class TestThinkingModelGetRecommendation:
    """Integration of the thinking-model branch inside get_recommendation."""

    def _build_thinking_adapter(self, transport: httpx.MockTransport) -> OllamaAdapter:
        return OllamaAdapter(
            model="deepseek-r1:14b",
            prompt=_make_prompt(),
            role="single",
            client=httpx.AsyncClient(transport=transport),
        )

    async def test_thinking_model_payload_drops_format_key(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            # Wrap a thinking-preamble + final JSON in the Ollama envelope.
            free_text = (
                "<think>\nVolatility is 0.0004 — relatively low.\n"
                "Widening spacing seems sensible.\n</think>\n\n"
                + json.dumps(
                    {
                        "role": "quant",
                        "recommendations": {"spacing_percentage": 1.2},
                        "rationale": "Low vol; widen modestly.",
                        "confidence": "medium",
                    }
                )
            )
            return httpx.Response(200, json=_ollama_response(free_text))

        adapter = self._build_thinking_adapter(httpx.MockTransport(handler))
        try:
            rec = await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

        # The thinking-model branch must NOT send `format: "json"`.
        body = captured["body"]
        assert isinstance(body, dict)
        assert "format" not in body, "thinking-model branch should drop format=json constraint"
        # Recommendation parsed from the trailing JSON.
        assert rec.recommendations == {"spacing_percentage": 1.2}
        assert rec.confidence == "medium"
        assert rec.rationale.startswith("Low vol")

    async def test_thinking_model_no_json_in_output_raises_advisor_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_ollama_response("<think>I cannot comply with that request.</think>"),
            )

        adapter = self._build_thinking_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(AdvisorError, match="no parseable JSON object"):
                await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

    async def test_non_thinking_model_payload_still_has_format_key(self) -> None:
        """Regression guard — phi4 / qwq / gemma3 must keep format=json."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_ollama_response(
                    {
                        "role": "quant",
                        "recommendations": {},
                        "rationale": "ok",
                        "confidence": "low",
                    }
                ),
            )

        adapter = OllamaAdapter(
            model="phi4:14b",
            prompt=_make_prompt(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await adapter.get_recommendation(_make_summary())
        finally:
            await adapter.aclose()

        body = captured["body"]
        assert isinstance(body, dict)
        assert body["format"] == "json"

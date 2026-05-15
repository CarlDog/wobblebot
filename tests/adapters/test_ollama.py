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

from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.config.prompts import Prompt, PromptMetadata
from wobblebot.ports.advisor import AdvisorRecommendation, PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


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

"""Tests for the LLM endpoint health probes (P3)."""

from __future__ import annotations

import httpx
import pytest

from wobblebot.services.llm_health import (
    LLMEndpoint,
    LLMHealthChecker,
    build_llm_endpoints,
    probe_llm_endpoints,
)

pytestmark = pytest.mark.unit


class TestBuildEndpoints:
    def test_only_configured_providers_appear(self) -> None:
        endpoints = build_llm_endpoints(
            ollama_base_url="http://nas:11434/",
            anthropic_key=None,
            openai_key="sk-x",
            google_key=None,
        )
        names = [e.name for e in endpoints]
        assert names == ["Ollama", "OpenAI"]
        assert endpoints[0].url == "http://nas:11434/api/tags"  # slash normalized

    def test_empty_string_keys_count_as_unset(self) -> None:
        """MCP-host lesson generalized: '' env values are not config."""
        assert (
            build_llm_endpoints(ollama_base_url="", anthropic_key="", openai_key="", google_key="")
            == []
        )

    def test_google_key_rides_a_header_never_the_url(self) -> None:
        (endpoint,) = build_llm_endpoints(
            ollama_base_url=None, anthropic_key=None, openai_key=None, google_key="g-secret"
        )
        assert "g-secret" not in endpoint.url
        assert ("x-goog-api-key", "g-secret") in endpoint.headers


@pytest.mark.asyncio
class TestProbe:
    async def test_2xx_is_ok_and_401_names_the_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "anthropic" in str(request.url):
                return httpx.Response(401)
            return httpx.Response(200, json={"models": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await probe_llm_endpoints(
                client,
                [
                    LLMEndpoint(name="Ollama", url="http://nas:11434/api/tags"),
                    LLMEndpoint(name="Anthropic", url="https://api.anthropic.com/v1/models"),
                ],
            )
        finally:
            await client.aclose()
        assert results[0].ok is True
        assert results[1].ok is False
        assert "key rejected" in results[1].detail

    async def test_transport_error_is_not_ok_with_type_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            (result,) = await probe_llm_endpoints(
                client, [LLMEndpoint(name="Ollama", url="http://nas:11434/api/tags")]
            )
        finally:
            await client.aclose()
        assert result.ok is False
        assert "ConnectError" in result.detail


@pytest.mark.asyncio
class TestCheckerTTL:
    async def test_second_get_within_ttl_hits_the_cache(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            checker = LLMHealthChecker(
                client,
                [LLMEndpoint(name="Ollama", url="http://nas:11434/api/tags")],
                ttl_seconds=60.0,
            )
            first = await checker.get()
            second = await checker.get()
        finally:
            await client.aclose()
        assert calls["n"] == 1
        assert first == second

    async def test_reset_forces_a_refresh(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            checker = LLMHealthChecker(
                client,
                [LLMEndpoint(name="Ollama", url="http://nas:11434/api/tags")],
                ttl_seconds=60.0,
            )
            await checker.get()
            checker.reset()
            await checker.get()
        finally:
            await client.aclose()
        assert calls["n"] == 2

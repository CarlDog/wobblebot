"""LLM endpoint health probes for the /health page (P3).

Mirrors ``kraken_health``: a TTL-cached checker mounted on
``app.state`` so dashboard refreshes don't hammer the vendors. Every
probe is the provider's cheapest **non-billable** "are you alive"
endpoint — Ollama ``/api/tags`` (no model load), the cloud providers'
free models-list GETs. Only endpoints that are actually configured
(key present after empty-string normalization) get probed; an
unconfigured provider simply doesn't appear.

Google deliberately authenticates via the ``x-goog-api-key`` header,
not the documented ``?key=`` query param — keys never belong in URLs
(they leak into logs and error strings).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LLMEndpoint:
    """One probe target: display name + GET url + auth headers."""

    name: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LLMEndpointHealth:
    """One endpoint's probe outcome for the /health view."""

    name: str
    ok: bool | None
    detail: str
    checked_at: datetime | None


def _normalize(value: str | None) -> str | None:
    """Empty-string env values count as unset (MCP-host lesson)."""
    return value if value else None


def build_llm_endpoints(
    *,
    ollama_base_url: str | None,
    anthropic_key: str | None,
    openai_key: str | None,
    google_key: str | None,
) -> list[LLMEndpoint]:
    """Probe list for whatever is actually configured."""
    endpoints: list[LLMEndpoint] = []
    ollama = _normalize(ollama_base_url)
    if ollama:
        endpoints.append(LLMEndpoint(name="Ollama", url=f"{ollama.rstrip('/')}/api/tags"))
    anthropic = _normalize(anthropic_key)
    if anthropic:
        endpoints.append(
            LLMEndpoint(
                name="Anthropic",
                url="https://api.anthropic.com/v1/models",
                headers=(("x-api-key", anthropic), ("anthropic-version", "2023-06-01")),
            )
        )
    openai = _normalize(openai_key)
    if openai:
        endpoints.append(
            LLMEndpoint(
                name="OpenAI",
                url="https://api.openai.com/v1/models",
                headers=(("Authorization", f"Bearer {openai}"),),
            )
        )
    google = _normalize(google_key)
    if google:
        endpoints.append(
            LLMEndpoint(
                name="Google",
                url="https://generativelanguage.googleapis.com/v1beta/models",
                headers=(("x-goog-api-key", google),),
            )
        )
    return endpoints


async def probe_llm_endpoints(
    client: httpx.AsyncClient,
    endpoints: list[LLMEndpoint],
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[LLMEndpointHealth, ...]:
    """GET each endpoint once; 2xx = healthy. Never raises.

    Failure detail carries only the HTTP status or exception type. Raw
    transport messages, URLs, response bodies and auth headers never enter
    persisted observations or the dashboard.
    """
    results: list[LLMEndpointHealth] = []
    for endpoint in endpoints:
        checked_at = datetime.now(UTC)
        try:
            response = await client.get(
                endpoint.url,
                headers=dict(endpoint.headers),
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            results.append(
                LLMEndpointHealth(
                    name=endpoint.name,
                    ok=False,
                    detail=type(exc).__name__,
                    checked_at=checked_at,
                )
            )
            continue
        ok = 200 <= response.status_code < 300
        detail = f"HTTP {response.status_code}"
        if response.status_code in (401, 403):
            detail += " — key rejected (rotated?)"
        results.append(
            LLMEndpointHealth(name=endpoint.name, ok=ok, detail=detail, checked_at=checked_at)
        )
    return tuple(results)


class LLMHealthChecker:
    """TTL-cached wrapper around :func:`probe_llm_endpoints`.

    Same shape as ``KrakenHealthProbe``: one instance on ``app.state``,
    an ``asyncio.Lock`` serializing refreshes, cache shared across
    requests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoints: list[LLMEndpoint],
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._endpoints = endpoints
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cached: tuple[LLMEndpointHealth, ...] | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> tuple[LLMEndpointHealth, ...]:
        """Current endpoint healths, refreshing when the cache ages out."""
        async with self._lock:
            now = datetime.now(UTC)
            if self._cached is not None and self._cached:
                checked_at = self._cached[0].checked_at
                age = (now - checked_at).total_seconds() if checked_at is not None else float("inf")
                if age < self._ttl_seconds:
                    return self._cached
            elif self._cached is not None:
                # Zero configured endpoints: nothing ever goes stale.
                return self._cached
            self._cached = await probe_llm_endpoints(
                self._client,
                self._endpoints,
                timeout_seconds=self._timeout_seconds,
            )
            return self._cached

    def reset(self) -> None:
        """Drop the cached value (test seam)."""
        self._cached = None


__all__ = (
    "LLMEndpoint",
    "LLMEndpointHealth",
    "LLMHealthChecker",
    "build_llm_endpoints",
    "probe_llm_endpoints",
)


class ProviderHealthReader(Protocol):
    """Common read contract for local probes and persisted daemon observations."""

    async def get(self) -> tuple[LLMEndpointHealth, ...]:
        """Read provider observations without granting a write capability."""

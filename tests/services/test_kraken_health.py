"""Tests for ``services/kraken_health.py``.

Wires ``httpx.MockTransport`` against the probe — same pattern as
``tests/adapters/test_kraken_adapter.py`` — to exercise every branch
of ``fetch_kraken_system_status`` plus the ``KrakenHealthProbe`` TTL
behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from wobblebot.services.kraken_health import (
    KrakenHealthProbe,
    KrakenHealthResult,
    KrakenSystemStatus,
    fetch_kraken_system_status,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Wire an ``httpx.AsyncClient`` against a ``MockTransport`` handler.

    No ``base_url`` here — the probe builds absolute URLs against
    ``api.kraken.com`` so tests don't need to know the route shape.
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _ok_envelope(status: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"error": [], "result": {"status": status, "timestamp": "2026-05-22T10:00:00Z"}},
    )


class TestFetchKrakenSystemStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("online", KrakenSystemStatus.ONLINE),
            ("maintenance", KrakenSystemStatus.MAINTENANCE),
            ("cancel_only", KrakenSystemStatus.CANCEL_ONLY),
            ("post_only", KrakenSystemStatus.POST_ONLY),
        ],
    )
    async def test_every_canonical_status_parses(
        self, raw: str, expected: KrakenSystemStatus
    ) -> None:
        client = _client_with(lambda req: _ok_envelope(raw))
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is expected
        assert result.error_message is None

    async def test_hits_correct_url(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return _ok_envelope("online")

        client = _client_with(handler)
        try:
            await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert seen == ["https://api.kraken.com/0/public/SystemStatus"]

    async def test_transport_error_returns_probe_failed(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("simulated DNS failure")

        client = _client_with(handler)
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message
        assert "DNS failure" in result.error_message

    async def test_5xx_returns_probe_failed(self) -> None:
        client = _client_with(lambda req: httpx.Response(503, text="upstream busy"))
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message

    async def test_non_dict_envelope_returns_probe_failed(self) -> None:
        client = _client_with(lambda req: httpx.Response(200, json=["unexpected", "shape"]))
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message == "unexpected envelope type: list"

    async def test_envelope_error_array_returns_probe_failed(self) -> None:
        client = _client_with(
            lambda req: httpx.Response(200, json={"error": ["EService:Unavailable"], "result": {}})
        )
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message is not None
        assert "EService:Unavailable" in result.error_message

    async def test_unrecognized_status_returns_probe_failed(self) -> None:
        client = _client_with(lambda req: _ok_envelope("zarjaz"))
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message is not None
        assert "zarjaz" in result.error_message

    async def test_malformed_json_returns_probe_failed(self) -> None:
        client = _client_with(lambda req: httpx.Response(200, content=b"not-json"))
        try:
            result = await fetch_kraken_system_status(client)
        finally:
            await client.aclose()
        assert result.status is KrakenSystemStatus.PROBE_FAILED
        assert result.error_message


class TestKrakenHealthProbeCache:
    async def test_first_call_fetches_then_caches(self) -> None:
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _ok_envelope("online")

        client = _client_with(handler)
        try:
            probe = KrakenHealthProbe(client, ttl_seconds=60.0)
            first = await probe.get()
            second = await probe.get()
        finally:
            await client.aclose()
        # Same cached result; one HTTP call.
        assert call_count["n"] == 1
        assert first is second  # cache returns identical object reference
        assert first.status is KrakenSystemStatus.ONLINE

    async def test_reset_forces_refetch(self) -> None:
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _ok_envelope("online")

        client = _client_with(handler)
        try:
            probe = KrakenHealthProbe(client, ttl_seconds=60.0)
            await probe.get()
            probe.reset()
            await probe.get()
        finally:
            await client.aclose()
        assert call_count["n"] == 2

    async def test_concurrent_gets_share_one_fetch(self) -> None:
        """asyncio.Lock serializes refreshes; concurrent cache-miss
        callers don't stampede the upstream."""
        call_count = {"n": 0}

        async def slow_responder(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            # Yield to the event loop so the second caller would have
            # a chance to start a parallel fetch if the lock weren't
            # serializing them.
            await asyncio.sleep(0.01)
            return _ok_envelope("online")

        transport = httpx.MockTransport(slow_responder)
        client = httpx.AsyncClient(transport=transport)
        try:
            probe = KrakenHealthProbe(client, ttl_seconds=60.0)
            results = await asyncio.gather(probe.get(), probe.get(), probe.get())
        finally:
            await client.aclose()
        # First call did the work; the other two saw the cached value.
        assert call_count["n"] == 1
        for r in results:
            assert r.status is KrakenSystemStatus.ONLINE

    async def test_ttl_expiry_triggers_refetch(self) -> None:
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _ok_envelope("online")

        client = _client_with(handler)
        try:
            # ttl_seconds=0 means "always stale" — every get() refetches.
            probe = KrakenHealthProbe(client, ttl_seconds=0.0)
            await probe.get()
            await probe.get()
        finally:
            await client.aclose()
        assert call_count["n"] == 2

    async def test_cached_value_is_KrakenHealthResult(self) -> None:
        client = _client_with(lambda req: _ok_envelope("maintenance"))
        try:
            probe = KrakenHealthProbe(client, ttl_seconds=60.0)
            result = await probe.get()
        finally:
            await client.aclose()
        assert isinstance(result, KrakenHealthResult)
        assert result.status is KrakenSystemStatus.MAINTENANCE

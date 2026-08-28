"""Nonce monotonicity across adapter instances sharing one API key.

Production regression, 2026-08-23. ``cli/maintenance`` runs its tasks
under one ``asyncio.gather`` and each builds its OWN ``KrakenAdapter``
from the reader key. The nonce counter lived on the instance, so the
adapters drew from independent counters and Kraken rejected the losers:

    capital report: balance fetch failed: ['EAPI:Invalid nonce']
    ledger sync:    fetch failed:        ['EAPI:Invalid nonce']

Both inside the same 300ms. It raced-and-usually-won with two such
tasks; the third tipped it over.

These tests construct SEPARATE adapters from ONE key -- the exact
production shape -- and assert the two properties the fix provides:
every nonce strictly increases, and requests on a key do not overlap.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig

pytestmark = pytest.mark.unit

_SECRET = "c2VjcmV0"  # base64("secret")


def _adapter(key: str, handler: Any) -> KrakenAdapter:
    client = httpx.AsyncClient(
        base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)
    )
    return KrakenAdapter(config=KrakenConfig(api_key=key, api_secret=_SECRET), http_client=client)


@pytest.mark.asyncio
class TestNonceAcrossInstances:
    async def test_concurrent_adapters_on_one_key_never_repeat_a_nonce(self) -> None:
        """The exact production failure. Three adapters, one key, fired
        together — every nonce must be unique AND increasing."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&") if "=" in pair
            )
            seen.append(int(body["nonce"]))
            return httpx.Response(200, json={"error": [], "result": {}})

        adapters = [_adapter("shared-reader-key", handler) for _ in range(3)]
        try:
            await asyncio.gather(
                *(a._private_post("/0/private/Balance") for a in adapters)  # noqa: SLF001
            )
        finally:
            for a in adapters:
                await a.aclose()

        assert len(seen) == 3
        assert len(set(seen)) == 3, f"duplicate nonce across instances: {seen}"
        assert seen == sorted(seen), f"nonces arrived out of order: {seen}"

    async def test_many_concurrent_calls_stay_strictly_increasing(self) -> None:
        """Same-millisecond collisions are the mechanism; 25 concurrent
        calls make them near-certain without the shared counter."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&") if "=" in pair
            )
            seen.append(int(body["nonce"]))
            return httpx.Response(200, json={"error": [], "result": {}})

        adapters = [_adapter("busy-key", handler) for _ in range(25)]
        try:
            await asyncio.gather(
                *(a._private_post("/0/private/Balance") for a in adapters)  # noqa: SLF001
            )
        finally:
            for a in adapters:
                await a.aclose()

        assert len(set(seen)) == 25, f"duplicates: {len(seen) - len(set(seen))}"
        assert seen == sorted(seen)

    async def test_requests_on_one_key_do_not_overlap(self) -> None:
        """Kraken validates against the highest nonce it has SEEN, so a
        correctly-generated lower nonce still fails if it lands second.
        Only serialization prevents that."""
        in_flight = 0
        max_in_flight = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json={"error": [], "result": {}})

        adapters = [_adapter("serial-key", slow_handler) for _ in range(4)]
        try:
            await asyncio.gather(
                *(a._private_post("/0/private/Balance") for a in adapters)  # noqa: SLF001
            )
        finally:
            for a in adapters:
                await a.aclose()

        assert max_in_flight == 1, f"{max_in_flight} concurrent requests on one key"

    async def test_different_keys_are_not_serialized(self) -> None:
        """The trader key must never queue behind the reader key's
        maintenance reads — that would put trading latency behind
        background housekeeping."""
        in_flight = 0
        max_in_flight = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json={"error": [], "result": {}})

        adapters = [_adapter(f"key-{i}", slow_handler) for i in range(4)]
        try:
            await asyncio.gather(
                *(a._private_post("/0/private/Balance") for a in adapters)  # noqa: SLF001
            )
        finally:
            for a in adapters:
                await a.aclose()

        assert max_in_flight > 1, "distinct keys were needlessly serialized"

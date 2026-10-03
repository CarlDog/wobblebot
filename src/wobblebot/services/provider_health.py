"""Provider health produced with existing daemon credentials, read without them."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from wobblebot.domain.provider_health import ProviderHealthSnapshot, ProviderObservation
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_health import LLMEndpoint, LLMEndpointHealth, probe_llm_endpoints

_LOGGER = logging.getLogger(__name__)


async def publish_provider_health(
    storage: StoragePort, client: httpx.AsyncClient, endpoints: list[LLMEndpoint]
) -> None:
    """Replace the complete snapshot, so removed credentials do not stay healthy."""
    observations = await probe_llm_endpoints(client, endpoints)
    await storage.save_provider_health(
        ProviderHealthSnapshot(
            producer="operator",
            checked_at=datetime.now(UTC),
            observations=tuple(
                ProviderObservation(
                    name=item.name,
                    ok=bool(item.ok),
                    detail=item.detail,
                    checked_at=item.checked_at,
                )
                for item in observations
                if item.checked_at is not None
            ),
        )
    )


async def run_provider_health(
    storage: StoragePort,
    endpoints: list[LLMEndpoint],
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 60.0,
) -> None:
    """Bounded probes; a persistence failure becomes stale evidence, never green."""
    if interval_seconds <= 0:
        await stop_event.wait()
        return
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            try:
                await publish_provider_health(storage, client, endpoints)
            except StorageError:
                _LOGGER.warning("provider health snapshot could not be persisted")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue


class PersistedProviderHealth:
    """The web reader has no HTTP client, endpoints, credentials or write path."""

    def __init__(self, storage: StoragePort, *, stale_after_seconds: float = 180.0) -> None:
        self._storage = storage
        self._stale_after = timedelta(seconds=stale_after_seconds)

    async def get(self) -> tuple[LLMEndpointHealth, ...]:
        """Return independently aged observations; missing evidence is unknown."""
        try:
            snapshots = await self._storage.get_provider_health()
        except StorageError:
            return (LLMEndpointHealth("Provider health", None, "Observation unavailable", None),)
        if not snapshots:
            return (LLMEndpointHealth("Provider health", None, "No daemon observation yet", None),)
        now = datetime.now(UTC)
        results: list[LLMEndpointHealth] = []
        for snapshot in snapshots:
            if not snapshot.observations:
                stale = not timedelta(0) <= now - snapshot.checked_at <= self._stale_after
                results.append(
                    LLMEndpointHealth(
                        f"Provider health ({snapshot.producer})",
                        None if stale else True,
                        "Stale configuration observation" if stale else "No endpoints configured",
                        snapshot.checked_at,
                    )
                )
            for item in snapshot.observations:
                stale = not timedelta(0) <= now - item.checked_at <= self._stale_after
                results.append(
                    LLMEndpointHealth(
                        f"{item.name} ({snapshot.producer})",
                        None if stale else item.ok,
                        "Stale observation; current health unknown" if stale else item.detail,
                        item.checked_at,
                    )
                )
        return tuple(results)

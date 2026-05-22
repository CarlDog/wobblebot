"""Kraken SystemStatus probe with TTL cache.

Stage 8.4.E health-icon work. The web layer's ``/health`` view (plus
the dashboard's traffic-light icon) needs to know whether Kraken is
serving trades right now — independent of any local daemon. This
probe hits the public ``/0/public/SystemStatus`` endpoint (no auth
required) and caches the result with a 60s TTL so dashboard
refreshes don't hammer Kraken.

Probe failures (timeout, 5xx, malformed envelope) return a
``PROBE_FAILED`` sentinel rather than raising — the upstream is
upstream, so the health view degrades gracefully if Kraken itself
is unreachable.

Design notes:

* **Standalone from ``KrakenAdapter``.** This service must not drag
  trading credentials or the adapter's caches into the web layer.
  Pass an ``httpx.AsyncClient`` directly; the web app owns it.
* **TTL cache is in-process.** One :class:`KrakenHealthProbe`
  instance lives on ``app.state`` for the life of the FastAPI app.
  ``asyncio.Lock`` serializes concurrent dashboard refreshes so a
  cache-miss stampede can't fire N parallel probes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

import httpx

_KRAKEN_BASE_URL: Final[str] = "https://api.kraken.com"
_SYSTEM_STATUS_PATH: Final[str] = "/0/public/SystemStatus"
_DEFAULT_TTL_SECONDS: Final[float] = 60.0
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0


class KrakenSystemStatus(StrEnum):
    """Kraken ``/0/public/SystemStatus`` value plus a probe-failure sentinel.

    Canonical Kraken values per
    https://docs.kraken.com/rest/#tag/System-Status:

    * ``online`` — full trading
    * ``maintenance`` — no trading
    * ``cancel_only`` — only cancellations accepted
    * ``post_only`` — only post-only orders accepted

    ``probe_failed`` is local — surfaced when the HTTP probe itself
    can't complete (timeout, 5xx, malformed envelope, unknown
    status string). Distinct from ``maintenance`` so the dashboard
    can tell "Kraken is offline" from "we can't tell whether Kraken
    is offline."
    """

    ONLINE = "online"
    MAINTENANCE = "maintenance"
    CANCEL_ONLY = "cancel_only"
    POST_ONLY = "post_only"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True)
class KrakenHealthResult:
    """Outcome of one probe attempt.

    ``fetched_at`` is the wallclock at attempt time (whether the
    probe succeeded or not). ``error_message`` is non-None only when
    ``status == PROBE_FAILED`` — included so the ``/health`` page can
    surface why the probe failed without forcing the operator to
    tail logs.
    """

    status: KrakenSystemStatus
    fetched_at: datetime
    error_message: str | None = None


async def fetch_kraken_system_status(
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> KrakenHealthResult:
    """Issue one probe to Kraken's SystemStatus endpoint. Never raises.

    Returns a :class:`KrakenHealthResult`. On any error path (HTTP
    transport, non-2xx response, empty/missing result envelope,
    unrecognized status string), the result carries
    ``status=PROBE_FAILED`` plus a one-line ``error_message`` for the
    UI.

    Args:
        client: Caller-owned ``httpx.AsyncClient``. Tests inject one
            wired to ``httpx.MockTransport``; production injects the
            app-singleton client from ``app.state``.
        timeout_seconds: Per-request timeout. SystemStatus is a cheap
            unauthenticated GET; 5s is generous.
    """
    now = datetime.now(UTC)
    try:
        response = await client.get(
            f"{_KRAKEN_BASE_URL}{_SYSTEM_STATUS_PATH}",
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        envelope = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return KrakenHealthResult(
            status=KrakenSystemStatus.PROBE_FAILED,
            fetched_at=now,
            error_message=str(exc) or type(exc).__name__,
        )

    if not isinstance(envelope, dict):
        return KrakenHealthResult(
            status=KrakenSystemStatus.PROBE_FAILED,
            fetched_at=now,
            error_message=f"unexpected envelope type: {type(envelope).__name__}",
        )
    errors = envelope.get("error") or []
    if errors:
        return KrakenHealthResult(
            status=KrakenSystemStatus.PROBE_FAILED,
            fetched_at=now,
            error_message=f"Kraken envelope error: {errors}",
        )
    result = envelope.get("result") or {}
    raw_status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(raw_status, str):
        return KrakenHealthResult(
            status=KrakenSystemStatus.PROBE_FAILED,
            fetched_at=now,
            error_message=f"unrecognized Kraken status: {raw_status!r}",
        )
    try:
        status = KrakenSystemStatus(raw_status)
    except ValueError:
        return KrakenHealthResult(
            status=KrakenSystemStatus.PROBE_FAILED,
            fetched_at=now,
            error_message=f"unrecognized Kraken status: {raw_status!r}",
        )
    return KrakenHealthResult(status=status, fetched_at=now)


class KrakenHealthProbe:
    """TTL-cached wrapper around :func:`fetch_kraken_system_status`.

    Mount one instance on ``app.state`` per the FastAPI factory; the
    cache survives across requests so a busy dashboard doesn't probe
    Kraken on every refresh. ``asyncio.Lock`` serializes concurrent
    refreshes — a fresh-cache check is fast, a cold-cache fetch is
    one round-trip, and either way the second caller in flight sees
    the first caller's result.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cached: KrakenHealthResult | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> KrakenHealthResult:
        """Return the current Kraken status, refreshing if the cache is stale."""
        async with self._lock:
            now = datetime.now(UTC)
            if (
                self._cached is not None
                and (now - self._cached.fetched_at).total_seconds() < self._ttl_seconds
            ):
                return self._cached
            self._cached = await fetch_kraken_system_status(
                self._client,
                timeout_seconds=self._timeout_seconds,
            )
            return self._cached

    def reset(self) -> None:
        """Drop the cached value (test seam; not used in production)."""
        self._cached = None


__all__ = (
    "KrakenSystemStatus",
    "KrakenHealthResult",
    "fetch_kraken_system_status",
    "KrakenHealthProbe",
)

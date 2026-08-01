"""KrakenStatusAdapter — v1.1 ``NewsPort`` implementation for status.kraken.com.

Polls Kraken's own Statuspage.io-hosted incident feed
(``/api/v2/incidents.json``, public, no auth). Exchange-status
incidents (API degraded, a coin's deposits/withdrawals halted, trading
paused) are directly relevant news for a bot trading on Kraken -- this
feeds the same ``news_items`` pipeline the RSS/CryptoCompare adapters
do, tagged ``kraken_status`` so the advisor's news expert (and later a
parked auto-pause feature) can distinguish it from general market
news.

**One NewsItem per incident UPDATE, not per incident.** An incident
transitions through several states (investigating -> monitoring ->
resolved), each posted as its own ``incident_updates[]`` entry with a
stable id. Treating each transition as its own item lets the advisor
see an incident's progression over time -- storage's
``INSERT OR IGNORE`` dedup (keyed on ``(source, external_id)``) means
a single per-INCIDENT item would freeze at whichever state happened to
be current on the first poll that noticed it and never update to
"resolved".

**Coin extraction.** Kraken's own component names encode the ticker in
parentheses (e.g. ``"Digital Currency Funding - Ethereum (ETH) -
Polygon"``, ``"0x (ZRX) - Ethereum"``) -- verified against a live
``status.kraken.com/api/v2/summary.json`` + ``incidents.json``
response 2026-08-01, not documented anywhere, just Kraken's own naming
convention. Extracted from each update's ``affected_components``,
falling back to the parent incident's ``components`` when an update
carries none (the closing "resolved" update usually has
``affected_components: null``).

``incidents.json`` returns Statuspage's own bounded page (50 most
recent incidents as observed 2026-08-01, spanning ~2.5 months) -- no
client-side pagination or lookback window needed; the source itself
bounds the fetch.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NewsError
from wobblebot.ports.news import NewsPort

_DEFAULT_BASE_URL = "https://status.kraken.com"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_SOURCE_ID = "kraken_status"

_TICKER_IN_PARENS = re.compile(r"\(([A-Z][A-Z0-9]{1,5})\)")


def _extract_coins_from_components(components: Any) -> list[str]:
    """Pull ticker codes from a list of Statuspage component dicts."""
    if not isinstance(components, list):
        return []
    seen: dict[str, None] = {}
    for component in components:
        name = component.get("name") if isinstance(component, dict) else None
        if not isinstance(name, str):
            continue
        for match in _TICKER_IN_PARENS.findall(name):
            seen[match] = None
    return list(seen)


class KrakenStatusAdapter(NewsPort):
    """Kraken's own exchange-status incident feed, surfaced as a ``NewsPort``.

    Args:
        base_url: Status page base URL. Override for testing.
        timeout_seconds: HTTP read timeout.
        client: Optional pre-built ``httpx.AsyncClient`` (test seam).
            If ``None``, the adapter owns one and ``aclose()`` releases it.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    async def aclose(self) -> None:
        """Release the owned httpx client, if any."""
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[NewsItem]:
        try:
            response = await self._client.get(f"{self._base_url}/api/v2/incidents.json")
            response.raise_for_status()
            envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise NewsError(f"Kraken status fetch failed: {exc}") from exc

        raw_incidents = envelope.get("incidents")
        if not isinstance(raw_incidents, list):
            raise NewsError(
                "Kraken status envelope missing 'incidents' list; "
                f"got {type(raw_incidents).__name__}"
            )

        now_ts = Timestamp(dt=datetime.now(UTC))
        items: list[NewsItem] = []
        for incident in raw_incidents:
            items.extend(_incident_to_news_items(incident, now_ts))
        # Sort ASC by published_at -- port contract is oldest-first.
        items.sort(key=lambda it: it.published_at.dt)
        return items


def _incident_to_news_items(incident: Any, fetched_at: Timestamp) -> list[NewsItem]:
    """Map one incident's updates into NewsItems. Skips unusable rows."""
    if not isinstance(incident, dict):
        return []
    name = incident.get("name")
    if not isinstance(name, str) or not name.strip():
        return []
    shortlink = incident.get("shortlink")
    url = shortlink if isinstance(shortlink, str) and shortlink else None
    updates = incident.get("incident_updates")
    if not isinstance(updates, list):
        return []

    items: list[NewsItem] = []
    for update in updates:
        item = _update_to_news_item(
            name,
            update,
            fallback_components=incident.get("components"),
            url=url,
            fetched_at=fetched_at,
        )
        if item is not None:
            items.append(item)
    return items


def _update_to_news_item(
    incident_name: str,
    update: Any,
    *,
    fallback_components: Any,
    url: str | None,
    fetched_at: Timestamp,
) -> NewsItem | None:
    if not isinstance(update, dict):
        return None
    update_id = update.get("id")
    status = update.get("status")
    created_at = update.get("created_at")
    if not isinstance(update_id, str) or not update_id:
        return None
    if not isinstance(status, str) or not status:
        return None
    if not isinstance(created_at, str):
        return None
    try:
        # Python 3.11+'s fromisoformat accepts a trailing "Z" natively.
        published = datetime.fromisoformat(created_at)
    except ValueError:
        return None

    body = update.get("body")
    body_text = body.strip() if isinstance(body, str) else ""
    affected = update.get("affected_components")
    components = affected if isinstance(affected, list) and affected else fallback_components

    return NewsItem(
        source=_SOURCE_ID,
        external_id=update_id,
        published_at=Timestamp(dt=published),
        headline=f"{incident_name} — {status}",
        body=body_text,
        sentiment_score=None,
        mentioned_coins=_extract_coins_from_components(components),
        fetched_at=fetched_at,
        # Kraken's own status page IS the publisher -- source_id
        # already names it, matching the RSS adapter's same reasoning.
        publisher=None,
        url=url,
    )

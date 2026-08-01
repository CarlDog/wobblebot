"""Unit tests for KrakenStatusAdapter (v1.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from wobblebot.adapters.kraken_status_news import (
    KrakenStatusAdapter,
    _extract_coins_from_components,
)
from wobblebot.ports.exceptions import NewsError

pytestmark = pytest.mark.unit


def _component(name: str, *, code: str = "abc123") -> dict[str, Any]:
    return {"code": code, "name": name, "old_status": "operational", "new_status": "operational"}


def _update(
    *,
    update_id: str = "upd-1",
    status: str = "investigating",
    body: str = "We are investigating an issue.",
    created_at: str = "2026-07-31T16:03:45.870Z",
    affected_components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": update_id,
        "status": status,
        "body": body,
        "incident_id": "inc-1",
        "created_at": created_at,
        "updated_at": created_at,
        "display_at": created_at,
        "affected_components": affected_components,
        "deliver_notifications": True,
        "custom_tweet": None,
        "tweet_id": None,
    }


def _incident(
    *,
    incident_id: str = "inc-1",
    name: str = "Crypto Withdrawal Delays",
    status: str = "investigating",
    impact: str = "minor",
    shortlink: str = "https://stspg.io/abc123",
    updates: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": incident_id,
        "name": name,
        "status": status,
        "created_at": "2026-07-29T15:33:20.261Z",
        "updated_at": "2026-07-31T16:06:13.676Z",
        "monitoring_at": None,
        "resolved_at": None,
        "impact": impact,
        "shortlink": shortlink,
        "started_at": "2026-07-29T15:33:20.253Z",
        "page_id": "lfz25gyhcpjf",
        "incident_updates": updates if updates is not None else [_update()],
        "components": components if components is not None else [],
        "reminder_intervals": None,
    }


def _envelope(*incidents: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": {
            "id": "lfz25gyhcpjf",
            "name": "Kraken",
            "url": "https://status.kraken.com",
            "time_zone": "Etc/UTC",
            "updated_at": "2026-08-01T03:19:24.842Z",
        },
        "incidents": list(incidents),
    }


def _build_adapter(transport: httpx.MockTransport) -> KrakenStatusAdapter:
    return KrakenStatusAdapter(client=httpx.AsyncClient(transport=transport))


class TestExtractCoinsFromComponents:
    def test_single_ticker_in_parens(self) -> None:
        components = [_component("Digital Currency Funding - Ethereum (ETH) - Polygon")]
        assert _extract_coins_from_components(components) == ["ETH"]

    def test_multiple_components_dedup_preserves_order(self) -> None:
        components = [
            _component("Digital Currency Funding - Sui (SUI)"),
            _component("Digital Currency Funding - USDC (USDC) - Sui"),
            _component("Digital Currency Funding - Sui (SUI)"),  # duplicate
        ]
        assert _extract_coins_from_components(components) == ["SUI", "USDC"]

    def test_component_with_no_parens_ignored(self) -> None:
        assert _extract_coins_from_components([_component("Prop Trading")]) == []
        assert _extract_coins_from_components([_component("REST")]) == []

    def test_none_returns_empty(self) -> None:
        assert _extract_coins_from_components(None) == []

    def test_non_list_returns_empty(self) -> None:
        assert _extract_coins_from_components("not-a-list") == []

    def test_component_missing_name_skipped(self) -> None:
        assert _extract_coins_from_components([{"code": "x"}]) == []

    def test_non_dict_component_skipped(self) -> None:
        assert _extract_coins_from_components(["not-a-dict"]) == []


@pytest.mark.asyncio
class TestFetchHappyPath:
    async def test_single_incident_single_update(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_envelope(_incident()))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert len(items) == 1
        got = items[0]
        assert got.source == "kraken_status"
        assert got.external_id == "upd-1"
        assert got.headline == "Crypto Withdrawal Delays — investigating"
        assert got.body == "We are investigating an issue."
        assert got.published_at.dt == datetime(2026, 7, 31, 16, 3, 45, 870000, tzinfo=UTC)
        assert got.sentiment_score is None
        assert got.url == "https://stspg.io/abc123"
        assert got.publisher is None
        assert "status.kraken.com/api/v2/incidents.json" in captured["url"]

    async def test_one_news_item_per_incident_update(self) -> None:
        """An incident with 3 updates (investigating -> monitoring ->
        resolved) yields 3 distinct NewsItems, not 1."""
        incident = _incident(
            updates=[
                _update(update_id="u1", status="investigating"),
                _update(update_id="u2", status="monitoring"),
                _update(update_id="u3", status="resolved", body="This incident has been resolved."),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert len(items) == 3
        assert {it.external_id for it in items} == {"u1", "u2", "u3"}
        statuses = {it.external_id: it.headline.rsplit("— ", 1)[1] for it in items}
        assert statuses == {"u1": "investigating", "u2": "monitoring", "u3": "resolved"}

    async def test_multiple_incidents_sorted_ascending(self) -> None:
        older = _incident(
            incident_id="old",
            name="Older Incident",
            updates=[_update(update_id="old-u", created_at="2026-07-01T00:00:00.000Z")],
        )
        newer = _incident(
            incident_id="new",
            name="Newer Incident",
            updates=[_update(update_id="new-u", created_at="2026-07-31T00:00:00.000Z")],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(newer, older))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert [it.external_id for it in items] == ["old-u", "new-u"]

    async def test_no_incidents_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope())

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_base_url_used_for_request(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_envelope())

        adapter = KrakenStatusAdapter(
            base_url="https://status.example.com",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        assert captured["url"] == "https://status.example.com/api/v2/incidents.json"


@pytest.mark.asyncio
class TestCoinExtractionFromUpdates:
    async def test_uses_update_affected_components_when_present(self) -> None:
        incident = _incident(
            components=[_component("Digital Currency Funding - Bitcoin (BTC)")],
            updates=[
                _update(
                    affected_components=[_component("Digital Currency Funding - Ethereum (ETH)")]
                )
            ],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        # Update's own affected_components wins over the incident-level fallback.
        assert items[0].mentioned_coins == ["ETH"]

    async def test_falls_back_to_incident_components_when_update_has_none(self) -> None:
        """The closing 'resolved' update typically has
        affected_components=null -- must fall back to the incident's
        own components list rather than losing the coin tag."""
        incident = _incident(
            components=[_component("Digital Currency Funding - Bitcoin (BTC)")],
            updates=[_update(status="resolved", affected_components=None)],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].mentioned_coins == ["BTC"]

    async def test_no_components_anywhere_yields_empty_coins(self) -> None:
        incident = _incident(
            name="Website",
            components=[],
            updates=[_update(affected_components=None)],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].mentioned_coins == []


@pytest.mark.asyncio
class TestFetchErrorPaths:
    async def test_http_500_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="down")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="Kraken status fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_connection_error_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns refused")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="Kraken status fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_missing_incidents_list_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"page": {}})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="missing 'incidents' list"):
                await adapter.fetch()
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestRowMapping:
    async def test_incident_without_name_skipped(self) -> None:
        bad = _incident(name="")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(bad))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_update_without_created_at_skipped(self) -> None:
        bad_update = _update()
        del bad_update["created_at"]
        incident = _incident(updates=[bad_update])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_update_with_malformed_created_at_skipped(self) -> None:
        bad_update = _update(created_at="not-a-timestamp")
        incident = _incident(updates=[bad_update])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_update_without_id_skipped(self) -> None:
        bad_update = _update()
        del bad_update["id"]
        incident = _incident(updates=[bad_update])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_incident_without_shortlink_yields_null_url(self) -> None:
        incident = _incident(shortlink="")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].url is None

    async def test_empty_body_preserved_as_empty_string(self) -> None:
        incident = _incident(updates=[_update(body="")])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].body == ""

    async def test_incident_with_no_updates_list_skipped(self) -> None:
        incident = _incident()
        incident["incident_updates"] = None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(incident))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []


class TestConstructorAndLifecycle:
    def test_source_id_constant(self) -> None:
        assert KrakenStatusAdapter().source_id == "kraken_status"

    @pytest.mark.asyncio
    async def test_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        adapter = KrakenStatusAdapter(client=client)
        await adapter.aclose()
        assert not client.is_closed
        await client.aclose()

    def test_base_url_trailing_slash_stripped(self) -> None:
        adapter = KrakenStatusAdapter(base_url="https://status.kraken.com/")
        assert adapter._base_url == "https://status.kraken.com"  # pylint: disable=protected-access

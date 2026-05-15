"""Unit tests for CryptoCompareAdapter (Stage 3.2.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from wobblebot.adapters.cryptocompare_news import (
    CryptoCompareAdapter,
    _extract_coins_from_categories,
)
from wobblebot.ports.exceptions import NewsError

pytestmark = pytest.mark.unit


def _envelope(*items: dict[str, Any], envelope_type: int = 100) -> dict[str, Any]:
    return {
        "Type": envelope_type,
        "Message": "News list successfully returned",
        "Promoted": [],
        "Data": list(items),
    }


def _raw_item(
    *,
    article_id: str = "1001",
    title: str = "Bitcoin Hits New Highs",
    body: str = "BTC reached new highs today.",
    published_on: int | float = 1747332000,  # 2025-05-15T18:00:00 UTC
    categories: str = "BTC|ETH|Trading",
) -> dict[str, Any]:
    return {
        "id": article_id,
        "guid": f"https://example.com/article-{article_id}",
        "published_on": published_on,
        "title": title,
        "body": body,
        "tags": "Bitcoin|ETF|Markets",
        "lang": "EN",
        "upvotes": "10",
        "downvotes": "2",
        "categories": categories,
        "source_info": {"name": "CoinDesk", "lang": "EN", "img": "..."},
        "source": "coindesk",
    }


def _build_adapter(transport: httpx.MockTransport) -> CryptoCompareAdapter:
    return CryptoCompareAdapter(
        api_key="test-key-123",
        client=httpx.AsyncClient(transport=transport),
    )


class TestExtractCoinsFromCategories:
    def test_simple_pipe_separated(self) -> None:
        assert _extract_coins_from_categories("BTC|ETH|SOL") == ["BTC", "ETH", "SOL"]

    def test_filters_out_topic_tags(self) -> None:
        # Trading / Mining / Regulation aren't ticker-shaped.
        assert _extract_coins_from_categories("BTC|Trading|ETH|Mining") == ["BTC", "ETH"]

    def test_handles_whitespace(self) -> None:
        assert _extract_coins_from_categories(" BTC | ETH ") == ["BTC", "ETH"]

    def test_empty_string_returns_empty(self) -> None:
        assert _extract_coins_from_categories("") == []

    def test_only_topics_returns_empty(self) -> None:
        assert _extract_coins_from_categories("Trading|Mining|Regulation") == []

    def test_lowercase_rejected(self) -> None:
        # Tickers should be uppercase; lowercase is suspect.
        assert _extract_coins_from_categories("btc|ETH") == ["ETH"]


@pytest.mark.asyncio
class TestFetchHappyPath:
    async def test_single_item(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_envelope(_raw_item()))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert len(items) == 1
        got = items[0]
        assert got.source == "cryptocompare"
        assert got.external_id == "1001"
        assert got.headline == "Bitcoin Hits New Highs"
        assert got.body == "BTC reached new highs today."
        assert got.published_at.dt == datetime(2025, 5, 15, 18, 0, tzinfo=UTC)
        assert got.sentiment_score is None  # no reliable sentiment in CryptoCompare
        assert got.mentioned_coins == ["BTC", "ETH"]
        # Request shape
        assert "min-api.cryptocompare.com" in captured["url"]
        assert "lang=EN" in captured["url"]
        assert captured["headers"]["authorization"] == "Apikey test-key-123"

    async def test_multiple_items_sorted_ascending(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_envelope(
                    _raw_item(article_id="c", title="Newest", published_on=1747332000 + 120),
                    _raw_item(article_id="b", title="Middle", published_on=1747332000 + 60),
                    _raw_item(article_id="a", title="Oldest", published_on=1747332000),
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert [it.headline for it in items] == ["Oldest", "Middle", "Newest"]

    async def test_empty_data_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope())

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_lang_param_passed_through(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_envelope())

        adapter = CryptoCompareAdapter(
            api_key="k",
            lang="ES",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        assert "lang=ES" in captured["url"]

    async def test_categories_filter_passed_through(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_envelope())

        adapter = CryptoCompareAdapter(
            api_key="k",
            categories="BTC|ETH",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        # httpx URL-encodes pipes; check the decoded form
        assert "categories=BTC" in captured["url"]

    async def test_no_categories_param_when_unset(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_envelope())

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        assert "categories" not in captured["url"]

    async def test_api_key_in_header_not_query(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_envelope())

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        # Key MUST be in header, not URL (avoid upstream-log exposure).
        assert "test-key-123" not in captured["url"]
        assert captured["headers"]["authorization"] == "Apikey test-key-123"


@pytest.mark.asyncio
class TestFetchErrorPaths:
    async def test_http_500_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="CryptoCompare fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_connection_error_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns refused")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="CryptoCompare fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_envelope_type_not_100_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Type": 99, "Message": "bad key", "Data": []})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="unexpected envelope type"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_missing_data_list_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Type": 100, "Message": "ok"})

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="missing 'Data' list"):
                await adapter.fetch()
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestRowMapping:
    async def test_item_without_title_skipped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_envelope(
                    _raw_item(article_id="bad", title=""),
                    _raw_item(article_id="good", title="Has title"),
                ),
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert [it.external_id for it in items] == ["good"]

    async def test_item_without_published_on_skipped(self) -> None:
        bad = _raw_item(article_id="bad")
        del bad["published_on"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(bad))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_item_with_non_numeric_published_on_skipped(self) -> None:
        bad = _raw_item(article_id="bad")
        bad["published_on"] = "not-a-timestamp"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(bad))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_missing_id_yields_null_external_id(self) -> None:
        no_id = _raw_item()
        del no_id["id"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(no_id))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert len(items) == 1
        assert items[0].external_id is None

    async def test_empty_body_preserved_as_empty_string(self) -> None:
        item = _raw_item(body="")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope(item))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].body == ""


class TestConstructorAndLifecycle:
    def test_empty_api_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            CryptoCompareAdapter(api_key="")

    def test_source_id_constant(self) -> None:
        adapter = CryptoCompareAdapter(api_key="k")
        assert adapter.source_id == "cryptocompare"

    @pytest.mark.asyncio
    async def test_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        adapter = CryptoCompareAdapter(api_key="k", client=client)
        await adapter.aclose()
        assert not client.is_closed
        await client.aclose()

    def test_base_url_trailing_slash_stripped(self) -> None:
        adapter = CryptoCompareAdapter(api_key="k", base_url="https://example.com/")
        assert adapter._base_url == "https://example.com"  # pylint: disable=protected-access

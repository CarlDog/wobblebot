"""Unit tests for RssNewsAdapter (Stage 3.2.5).

The HTTP layer is mocked via ``httpx.MockTransport`` so tests stay
deterministic and never touch a real feed. Each test controls the
exact RSS/Atom XML the adapter parses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wobblebot.adapters.rss_news import RssNewsAdapter, _extract_mentioned_coins
from wobblebot.ports.exceptions import NewsError

pytestmark = pytest.mark.unit


RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com/</link>
    <description>A test feed for unit tests.</description>
    {items}
  </channel>
</rss>
"""

ATOM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <updated>2026-05-15T12:00:00Z</updated>
  <id>urn:example:test</id>
  {items}
</feed>
"""


def _rss_item(
    *,
    title: str = "BTC rallies to new highs",
    description: str = "Body text about Bitcoin.",
    guid: str | None = "guid-abc",
    pub_date: str = "Wed, 15 May 2026 10:00:00 +0000",
    link: str | None = "https://example.com/article-abc",
) -> str:
    guid_tag = f"<guid>{guid}</guid>" if guid else ""
    link_tag = f"<link>{link}</link>" if link else ""
    return f"""
    <item>
      <title>{title}</title>
      <description>{description}</description>
      <pubDate>{pub_date}</pubDate>
      {guid_tag}
      {link_tag}
    </item>"""


def _build_adapter(transport: httpx.MockTransport) -> RssNewsAdapter:
    return RssNewsAdapter(
        source_id="rss:test",
        feed_url="https://example.com/feed.xml",
        client=httpx.AsyncClient(transport=transport),
    )


class TestExtractMentionedCoins:
    def test_extracts_btc_from_headline(self) -> None:
        assert _extract_mentioned_coins("BTC rallies on ETF approval", "") == ["BTC"]

    def test_extracts_full_names(self) -> None:
        assert _extract_mentioned_coins("Bitcoin and Ethereum lead", "") == ["BTC", "ETH"]

    def test_case_insensitive(self) -> None:
        assert _extract_mentioned_coins("bitcoin slips", "") == ["BTC"]

    def test_dedups_across_headline_and_body(self) -> None:
        # BTC appears in both — only one entry in result.
        result = _extract_mentioned_coins("BTC rallies", "More on Bitcoin")
        assert result == ["BTC"]

    def test_returns_in_whitelist_order(self) -> None:
        # SOL is listed after BTC in the whitelist, so BTC comes first
        # even though the headline mentions SOL first.
        result = _extract_mentioned_coins("Solana surges; Bitcoin follows", "")
        assert result == ["BTC", "SOL"]

    def test_no_matches_returns_empty(self) -> None:
        assert _extract_mentioned_coins("SEC issues guidance", "") == []

    def test_word_boundaries_prevent_false_positives(self) -> None:
        # SOLicit / SOLid / Bitcoinx must NOT match.
        assert _extract_mentioned_coins("solicit a solid response", "") == []
        assert _extract_mentioned_coins("Bitcoinx fork", "") == []


@pytest.mark.asyncio
class TestFetchHappyPath:
    async def test_single_item(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=RSS_TEMPLATE.format(items=_rss_item()).encode("utf-8"),
                headers={"Content-Type": "application/rss+xml"},
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert len(items) == 1
        got = items[0]
        assert got.source == "rss:test"
        assert got.external_id == "guid-abc"
        assert got.headline == "BTC rallies to new highs"
        assert got.body == "Body text about Bitcoin."
        assert got.published_at.dt == datetime(2026, 5, 15, 10, 0, tzinfo=UTC)
        assert got.sentiment_score is None
        assert got.mentioned_coins == ["BTC"]
        # RSS feeds are the publisher (rss:test names them directly);
        # publisher stays None so consumers can distinguish direct-feed
        # items from aggregator-attributed ones.
        assert got.publisher is None
        # link → url for click-through in the web UI.
        assert got.url == "https://example.com/article-abc"

    async def test_multiple_items_sorted_ascending(self) -> None:
        # Feed orders newest-first; our port contract says return ASC.
        body = RSS_TEMPLATE.format(
            items="".join(
                [
                    _rss_item(
                        title="Item C newest",
                        guid="c",
                        pub_date="Wed, 15 May 2026 12:00:00 +0000",
                    ),
                    _rss_item(
                        title="Item B middle",
                        guid="b",
                        pub_date="Wed, 15 May 2026 11:00:00 +0000",
                    ),
                    _rss_item(
                        title="Item A oldest",
                        guid="a",
                        pub_date="Wed, 15 May 2026 10:00:00 +0000",
                    ),
                ]
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body.encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()

        assert [it.headline for it in items] == [
            "Item A oldest",
            "Item B middle",
            "Item C newest",
        ]

    async def test_empty_feed_returns_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=RSS_TEMPLATE.format(items="").encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_user_agent_header_sent(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["user-agent"] = request.headers.get("user-agent", "")
            return httpx.Response(200, content=RSS_TEMPLATE.format(items="").encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            await adapter.fetch()
        finally:
            await adapter.aclose()
        assert "wobblebot" in captured["user-agent"]

    async def test_atom_feed_parses(self) -> None:
        atom_entry = """
        <entry>
          <id>urn:atom:1</id>
          <title>Ethereum upgrade ships</title>
          <summary>Body about ETH.</summary>
          <updated>2026-05-15T10:00:00Z</updated>
        </entry>"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=ATOM_TEMPLATE.format(items=atom_entry).encode("utf-8")
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert len(items) == 1
        assert items[0].headline == "Ethereum upgrade ships"
        assert items[0].mentioned_coins == ["ETH"]

    async def test_entry_without_pubdate_skipped(self) -> None:
        # An entry with no timestamp can't be deduped or ordered; we skip it
        # rather than fail the whole batch.
        no_date_item = """
        <item>
          <title>Untimestamped</title>
          <description>No pub date.</description>
          <guid>no-date</guid>
        </item>"""
        good_item = _rss_item(title="Has date", guid="good")
        body = RSS_TEMPLATE.format(items=no_date_item + good_item)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body.encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert [it.headline for it in items] == ["Has date"]

    async def test_entry_without_title_skipped(self) -> None:
        no_title_item = """
        <item>
          <description>No title here.</description>
          <pubDate>Wed, 15 May 2026 10:00:00 +0000</pubDate>
          <guid>no-title</guid>
        </item>"""
        body = RSS_TEMPLATE.format(items=no_title_item)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body.encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items == []

    async def test_entry_without_guid_uses_link(self) -> None:
        item = """
        <item>
          <title>No GUID, but has link</title>
          <link>https://example.com/article-1</link>
          <description>Body.</description>
          <pubDate>Wed, 15 May 2026 10:00:00 +0000</pubDate>
        </item>"""
        body = RSS_TEMPLATE.format(items=item)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body.encode("utf-8"))

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            items = await adapter.fetch()
        finally:
            await adapter.aclose()
        assert items[0].external_id == "https://example.com/article-1"


@pytest.mark.asyncio
class TestFetchErrorPaths:
    async def test_http_500_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"upstream broken")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="RSS fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_connection_error_wraps_as_news_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns refused")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="RSS fetch failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()

    async def test_malformed_xml_with_zero_entries_raises(self) -> None:
        # A response that's not parseable as RSS/Atom at all should
        # surface as NewsError so the operator can investigate.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"this is not xml at all")

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsError, match="RSS parse failed"):
                await adapter.fetch()
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
class TestSourceIdAndLifecycle:
    async def test_source_id_property(self) -> None:
        adapter = RssNewsAdapter(source_id="rss:coindesk", feed_url="https://example.com/feed")
        try:
            assert adapter.source_id == "rss:coindesk"
        finally:
            await adapter.aclose()

    async def test_empty_source_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_id"):
            RssNewsAdapter(source_id="", feed_url="https://example.com/feed")

    async def test_empty_feed_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="feed_url"):
            RssNewsAdapter(source_id="rss:x", feed_url="")

    async def test_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        adapter = RssNewsAdapter(
            source_id="rss:x", feed_url="https://example.com/feed", client=client
        )
        await adapter.aclose()
        assert not client.is_closed
        await client.aclose()

    async def test_published_at_within_recent_window(self) -> None:
        """Sanity — fetched_at should be 'now-ish'."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=RSS_TEMPLATE.format(items=_rss_item()).encode("utf-8")
            )

        adapter = _build_adapter(httpx.MockTransport(handler))
        try:
            before = datetime.now(UTC)
            items = await adapter.fetch()
            after = datetime.now(UTC)
        finally:
            await adapter.aclose()
        assert before <= items[0].fetched_at.dt <= after + timedelta(seconds=1)

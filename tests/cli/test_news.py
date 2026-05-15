"""Tests for cli/news source-builder + per-source error isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from wobblebot.adapters.cryptocompare_news import CryptoCompareAdapter
from wobblebot.adapters.rss_news import RssNewsAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.news import _build_sources, _poll_source
from wobblebot.config.cli import CryptoCompareSpec, NewsConfig, RssFeedSpec
from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NewsError
from wobblebot.ports.news import NewsPort

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class _StubSource(NewsPort):
    """In-memory NewsPort that returns canned items or raises on demand."""

    def __init__(
        self,
        source_id: str,
        *,
        items: list[NewsItem] | None = None,
        error: NewsError | None = None,
    ) -> None:
        self._source_id = source_id
        self._items = items or []
        self._error = error

    @property
    def source_id(self) -> str:
        return self._source_id

    async def fetch(self) -> list[NewsItem]:
        if self._error is not None:
            raise self._error
        return list(self._items)


def _make_item(source: str, headline: str, external_id: str | None = "abc") -> NewsItem:
    return NewsItem(
        source=source,
        external_id=external_id,
        published_at=Timestamp(dt=datetime.now(UTC)),
        headline=headline,
    )


class TestBuildSources:
    async def test_no_sources_when_all_disabled(self) -> None:
        cfg = NewsConfig(
            rss_feeds=[
                RssFeedSpec(source_id="rss:a", url="https://a", enabled=False),
            ],
            cryptocompare=CryptoCompareSpec(enabled=False),
        )
        assert _build_sources(cfg) == []

    async def test_enabled_rss_feeds_only(self) -> None:
        cfg = NewsConfig(
            rss_feeds=[
                RssFeedSpec(source_id="rss:a", url="https://a/feed"),
                RssFeedSpec(source_id="rss:b", url="https://b/feed", enabled=False),
                RssFeedSpec(source_id="rss:c", url="https://c/feed"),
            ],
        )
        sources = _build_sources(cfg)
        try:
            assert [s.source_id for s in sources] == ["rss:a", "rss:c"]
            assert all(isinstance(s, RssNewsAdapter) for s in sources)
        finally:
            for src in sources:
                await src.aclose()  # type: ignore[attr-defined]

    async def test_cryptocompare_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CRYPTOCOMPARE_API_KEY", raising=False)
        cfg = NewsConfig(cryptocompare=CryptoCompareSpec(enabled=True))
        with pytest.raises(RuntimeError, match="CRYPTOCOMPARE_API_KEY"):
            _build_sources(cfg)

    async def test_cryptocompare_constructed_with_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", "test-key")
        cfg = NewsConfig(cryptocompare=CryptoCompareSpec(enabled=True, lang="EN"))
        sources = _build_sources(cfg)
        try:
            assert len(sources) == 1
            assert isinstance(sources[0], CryptoCompareAdapter)
            assert sources[0].source_id == "cryptocompare"
        finally:
            for src in sources:
                await src.aclose()  # type: ignore[attr-defined]

    async def test_mixed_rss_and_cryptocompare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", "test-key")
        cfg = NewsConfig(
            rss_feeds=[RssFeedSpec(source_id="rss:a", url="https://a/feed")],
            cryptocompare=CryptoCompareSpec(enabled=True),
        )
        sources = _build_sources(cfg)
        try:
            assert [s.source_id for s in sources] == ["rss:a", "cryptocompare"]
        finally:
            for src in sources:
                await src.aclose()  # type: ignore[attr-defined]


class TestPollSource:
    async def test_fetches_and_persists(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource(
            "rss:test",
            items=[
                _make_item("rss:test", "Headline one", external_id="1"),
                _make_item("rss:test", "Headline two", external_id="2"),
            ],
        )
        fetched, saved = await _poll_source(source, storage)
        assert fetched == 2
        assert saved == 2
        result = await storage.get_news_items()
        assert {it.headline for it in result} == {"Headline one", "Headline two"}

    async def test_news_error_returns_zero_zero(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource("rss:dead", error=NewsError("dns blew up"))
        fetched, saved = await _poll_source(source, storage)
        assert (fetched, saved) == (0, 0)

    async def test_empty_fetch_is_not_an_error(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource("rss:quiet", items=[])
        fetched, saved = await _poll_source(source, storage)
        assert (fetched, saved) == (0, 0)
        assert await storage.get_news_items() == []

    async def test_idempotent_across_polls(self, storage: SQLiteStorageAdapter) -> None:
        """Re-polling the same items doesn't multiply rows (storage dedup)."""
        item = _make_item("rss:test", "Same article", external_id="dup")
        source = _StubSource("rss:test", items=[item])
        await _poll_source(source, storage)
        await _poll_source(source, storage)
        await _poll_source(source, storage)
        assert len(await storage.get_news_items()) == 1

    async def test_one_bad_source_does_not_block_others(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Higher-level fault isolation: _poll_source swallows NewsError so
        the caller's outer loop continues with the next source."""
        bad = _StubSource("rss:bad", error=NewsError("blip"))
        good = _StubSource(
            "rss:good",
            items=[_make_item("rss:good", "good headline", external_id="g1")],
        )
        await _poll_source(bad, storage)  # Returns (0, 0), no raise
        await _poll_source(good, storage)
        items = await storage.get_news_items()
        assert len(items) == 1
        assert items[0].source == "rss:good"


# Reference to httpx so tests on Slice B/C don't unbalance imports.
_ = httpx

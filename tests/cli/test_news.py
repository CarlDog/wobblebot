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
from wobblebot.config.cli import (
    CryptoCompareSpec,
    NewsConfig,
    NewsDedupConfig,
    RssFeedSpec,
)
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


def _make_item(
    source: str,
    headline: str,
    external_id: str | None = "abc",
    coins: list[str] | None = None,
) -> NewsItem:
    return NewsItem(
        source=source,
        external_id=external_id,
        published_at=Timestamp(dt=datetime.now(UTC)),
        headline=headline,
        mentioned_coins=coins or [],
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
    """Stage 8.4: ``_poll_source`` now takes a NewsDedupConfig and
    returns a 3-tuple (fetched, saved, deduped). Tests use a
    ``fuzzy_threshold=0`` dedup config to disable the fuzzy layer —
    these tests target the fetch/save plumbing, not the dedup logic
    (which has its own test module in tests/services/test_news_dedup).
    """

    def _dedup_disabled(self) -> NewsDedupConfig:
        return NewsDedupConfig(window_hours=6.0, fuzzy_threshold=0.0)

    async def test_fetches_and_persists(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource(
            "rss:test",
            items=[
                _make_item("rss:test", "Headline one", external_id="1"),
                _make_item("rss:test", "Headline two", external_id="2"),
            ],
        )
        fetched, saved, deduped = await _poll_source(source, storage, self._dedup_disabled())
        assert fetched == 2
        assert saved == 2
        assert deduped == 0
        result = await storage.get_news_items()
        assert {it.headline for it in result} == {"Headline one", "Headline two"}

    async def test_news_error_returns_zero_zero(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource("rss:dead", error=NewsError("dns blew up"))
        fetched, saved, deduped = await _poll_source(source, storage, self._dedup_disabled())
        assert (fetched, saved, deduped) == (0, 0, 0)

    async def test_empty_fetch_is_not_an_error(self, storage: SQLiteStorageAdapter) -> None:
        source = _StubSource("rss:quiet", items=[])
        fetched, saved, deduped = await _poll_source(source, storage, self._dedup_disabled())
        assert (fetched, saved, deduped) == (0, 0, 0)
        assert await storage.get_news_items() == []

    async def test_idempotent_across_polls(self, storage: SQLiteStorageAdapter) -> None:
        """Re-polling the same items doesn't multiply rows (storage dedup)."""
        item = _make_item("rss:test", "Same article", external_id="dup")
        source = _StubSource("rss:test", items=[item])
        await _poll_source(source, storage, self._dedup_disabled())
        await _poll_source(source, storage, self._dedup_disabled())
        await _poll_source(source, storage, self._dedup_disabled())
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
        # Returns (0, 0, 0), no raise
        await _poll_source(bad, storage, self._dedup_disabled())
        await _poll_source(good, storage, self._dedup_disabled())
        items = await storage.get_news_items()
        assert len(items) == 1
        assert items[0].source == "rss:good"

    async def test_fuzzy_dedup_drops_syndicated_item(self, storage: SQLiteStorageAdapter) -> None:
        """Stage 8.4 fuzzy dedup: a second source's syndicated repost is
        dropped silently when similarity exceeds the threshold."""
        # First poll: CoinDesk publishes the wire story.
        coindesk = _StubSource(
            "rss:coindesk",
            items=[
                _make_item(
                    "rss:coindesk",
                    "Bitcoin Breaks $80k for First Time This Quarter",
                    external_id="cd-1",
                    coins=["BTC"],
                )
            ],
        )
        dedup = NewsDedupConfig(window_hours=6.0, fuzzy_threshold=60.0)
        fetched1, saved1, deduped1 = await _poll_source(coindesk, storage, dedup)
        assert (fetched1, saved1, deduped1) == (1, 1, 0)

        # Second poll: Decrypt republishes with reworded headline +
        # same BTC mention.
        decrypt = _StubSource(
            "rss:decrypt",
            items=[
                _make_item(
                    "rss:decrypt",
                    "BTC Surges Past $80K Milestone, First in Quarter",
                    external_id="dc-1",
                    coins=["BTC"],
                )
            ],
        )
        fetched2, saved2, deduped2 = await _poll_source(decrypt, storage, dedup)
        # Item fetched, but dedup catches it before save.
        assert fetched2 == 1
        assert saved2 == 0
        assert deduped2 == 1
        # Only the first source's row in storage.
        rows = await storage.get_news_items()
        assert len(rows) == 1
        assert rows[0].source == "rss:coindesk"


# Reference to httpx so tests on Slice B/C don't unbalance imports.
_ = httpx

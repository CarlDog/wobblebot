"""SQLiteStorageAdapter tests for the news-items persistence (Stage 3.2.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_item(
    *,
    source: str = "rss:coindesk",
    external_id: str | None = "abc123",
    minutes_ago: int = 5,
    headline: str = "BTC moves",
    body: str = "",
    sentiment: float | None = None,
    coins: list[str] | None = None,
) -> NewsItem:
    base = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return NewsItem(
        source=source,
        external_id=external_id,
        published_at=Timestamp(dt=base),
        headline=headline,
        body=body,
        sentiment_score=sentiment,
        mentioned_coins=coins or [],
    )


async def test_save_single_item_round_trips(storage: SQLiteStorageAdapter) -> None:
    item = _make_item(
        headline="BTC rallies on ETF approval",
        body="Body text here",
        sentiment=0.6,
        coins=["BTC", "ETH"],
    )
    await storage.save_news_item(item)
    result = await storage.get_news_items()
    assert len(result) == 1
    got = result[0]
    assert got.source == item.source
    assert got.external_id == item.external_id
    assert got.headline == item.headline
    assert got.body == item.body
    assert got.sentiment_score == pytest.approx(0.6)
    assert got.mentioned_coins == ["BTC", "ETH"]


async def test_save_is_idempotent_on_source_and_external_id(
    storage: SQLiteStorageAdapter,
) -> None:
    """Re-saving the same item across polls leaves storage in one row."""
    item = _make_item(external_id="dup-test-1")
    await storage.save_news_item(item)
    await storage.save_news_item(item)
    await storage.save_news_item(item)
    result = await storage.get_news_items()
    assert len(result) == 1


async def test_same_external_id_different_source_both_stored(
    storage: SQLiteStorageAdapter,
) -> None:
    """Dedup is scoped to (source, external_id), not external_id alone."""
    a = _make_item(source="rss:coindesk", external_id="shared-id")
    b = _make_item(source="rss:decrypt", external_id="shared-id")
    await storage.save_news_item(a)
    await storage.save_news_item(b)
    result = await storage.get_news_items()
    assert {it.source for it in result} == {"rss:coindesk", "rss:decrypt"}


async def test_null_external_id_does_not_dedup(storage: SQLiteStorageAdapter) -> None:
    """Items without a stable external_id always insert (no dedup possible)."""
    a = _make_item(external_id=None, headline="One")
    b = _make_item(external_id=None, headline="Two")
    await storage.save_news_item(a)
    await storage.save_news_item(b)
    result = await storage.get_news_items()
    assert len(result) == 2


async def test_default_order_is_published_descending(
    storage: SQLiteStorageAdapter,
) -> None:
    """Newest first — matches how advisor consumers want the data."""
    for off, ext in [(30, "old"), (10, "mid"), (1, "new")]:
        await storage.save_news_item(
            _make_item(external_id=ext, minutes_ago=off, headline=f"item-{ext}")
        )
    result = await storage.get_news_items()
    assert [it.headline for it in result] == ["item-new", "item-mid", "item-old"]


async def test_source_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_news_item(_make_item(source="rss:coindesk", external_id="a"))
    await storage.save_news_item(_make_item(source="cryptocompare", external_id="b"))
    result = await storage.get_news_items(source="cryptocompare")
    assert len(result) == 1
    assert result[0].source == "cryptocompare"


async def test_since_filter_inclusive(storage: SQLiteStorageAdapter) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for off, ext in [(0, "oldest"), (30, "mid"), (60, "newer"), (90, "newest")]:
        await storage.save_news_item(
            NewsItem(
                source="rss:test",
                external_id=ext,
                published_at=Timestamp(dt=base + timedelta(minutes=off)),
                headline=ext,
            )
        )
    cutoff = base + timedelta(minutes=30)
    result = await storage.get_news_items(since=cutoff)
    assert len(result) == 3
    assert all(it.published_at.dt >= cutoff for it in result)


async def test_until_filter_inclusive(storage: SQLiteStorageAdapter) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for off, ext in [(0, "oldest"), (30, "mid"), (60, "newest")]:
        await storage.save_news_item(
            NewsItem(
                source="rss:test",
                external_id=ext,
                published_at=Timestamp(dt=base + timedelta(minutes=off)),
                headline=ext,
            )
        )
    cutoff = base + timedelta(minutes=30)
    result = await storage.get_news_items(until=cutoff)
    assert len(result) == 2
    assert all(it.published_at.dt <= cutoff for it in result)


async def test_limit_caps_rows(storage: SQLiteStorageAdapter) -> None:
    for i in range(5):
        await storage.save_news_item(
            _make_item(external_id=f"e{i}", minutes_ago=i, headline=f"item-{i}")
        )
    result = await storage.get_news_items(limit=2)
    assert len(result) == 2
    # Limit takes the newest two (DESC order)
    assert result[0].headline == "item-0"
    assert result[1].headline == "item-1"


async def test_mentioned_coins_json_roundtrip(storage: SQLiteStorageAdapter) -> None:
    item = _make_item(coins=["BTC", "ETH", "DOGE", "ADA"])
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.mentioned_coins == ["BTC", "ETH", "DOGE", "ADA"]


async def test_empty_mentioned_coins(storage: SQLiteStorageAdapter) -> None:
    item = _make_item(coins=[])
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.mentioned_coins == []


async def test_sentiment_null_persists(storage: SQLiteStorageAdapter) -> None:
    item = _make_item(sentiment=None)
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.sentiment_score is None


async def test_sentiment_negative_persists(storage: SQLiteStorageAdapter) -> None:
    item = _make_item(sentiment=-0.75)
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.sentiment_score == pytest.approx(-0.75)


async def test_empty_returns_empty_list(storage: SQLiteStorageAdapter) -> None:
    assert await storage.get_news_items() == []

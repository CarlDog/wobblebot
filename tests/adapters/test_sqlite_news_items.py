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


async def test_publisher_and_url_round_trip(storage: SQLiteStorageAdapter) -> None:
    """publisher + url fields persist + return on read (2026-05-23 schema add)."""
    item = NewsItem(
        source="cryptocompare",
        external_id="cc-123",
        published_at=Timestamp(dt=datetime.now(UTC) - timedelta(minutes=2)),
        headline="BTC hits $100k via CoinDesk",
        publisher="CoinDesk",
        url="https://www.coindesk.com/markets/2026/05/23/btc-100k",
    )
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.publisher == "CoinDesk"
    assert got.url == "https://www.coindesk.com/markets/2026/05/23/btc-100k"


async def test_publisher_and_url_null_when_not_set(storage: SQLiteStorageAdapter) -> None:
    """Direct-RSS items have publisher=None; missing url stays None."""
    item = _make_item(source="rss:beincrypto", external_id="rss-x")
    await storage.save_news_item(item)
    got = (await storage.get_news_items())[0]
    assert got.publisher is None
    assert got.url is None


async def test_migration_adds_publisher_url_to_legacy_table(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An operator DB created before 2026-05-23 lacks publisher/url; the
    migration in connect() adds them without dropping data."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Build a pre-migration news_items table with the original column set.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE news_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            external_id     TEXT,
            published_at    TEXT NOT NULL,
            headline        TEXT NOT NULL,
            body            TEXT NOT NULL DEFAULT '',
            sentiment_score REAL,
            mentioned_coins TEXT NOT NULL DEFAULT '[]',
            fetched_at      TEXT NOT NULL,
            UNIQUE (source, external_id)
        )
        """)
    conn.execute(
        "INSERT INTO news_items "
        "(source, external_id, published_at, headline, body, mentioned_coins, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "rss:legacy",
            "old-1",
            "2026-05-01T00:00:00+00:00",
            "Legacy item",
            "",
            "[]",
            "2026-05-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    # Now open the same file via SQLiteStorageAdapter — the migration
    # in connect() must add the new columns.
    adapter = SQLiteStorageAdapter(str(db_path))
    await adapter.connect()
    try:
        items = await adapter.get_news_items()
        assert len(items) == 1
        # Legacy row still readable; new columns surface as None.
        assert items[0].headline == "Legacy item"
        assert items[0].publisher is None
        assert items[0].url is None
    finally:
        await adapter.close()

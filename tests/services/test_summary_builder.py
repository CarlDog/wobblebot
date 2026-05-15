"""Unit tests for SummaryBuilder (Stage 3.3 Slice B)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import NewsItem, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.advisor import CurrentGridParams
from wobblebot.services.summary_builder import SummaryBuilder

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed_prices(storage: SQLiteStorageAdapter, symbol: Symbol = BTC_USD) -> None:
    """Plant 5 snapshots over the last 20 minutes."""
    now = datetime.now(UTC)
    for off, amount in [(20, "100"), (15, "105"), (10, "103"), (5, "110"), (1, "108")]:
        await storage.save_price_snapshot(
            symbol,
            Price(amount=Decimal(amount), currency="USD"),
            Timestamp(dt=now - timedelta(minutes=off)),
        )


async def _seed_trades(storage: SQLiteStorageAdapter) -> None:
    """A matched buy + sell cycle within the lookback."""
    base = datetime.now(UTC) - timedelta(minutes=10)
    await storage.save_trade(
        Trade(
            id="t-buy",
            order_id="o-buy",
            symbol=BTC_USD,
            side=OrderSide.BUY,
            price=Price(amount=Decimal("100"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            fee=Decimal("0.04"),
            cost=Decimal("10"),
            executed_at=Timestamp(dt=base),
        )
    )
    await storage.save_trade(
        Trade(
            id="t-sell",
            order_id="o-sell",
            symbol=BTC_USD,
            side=OrderSide.SELL,
            price=Price(amount=Decimal("110"), currency="USD"),
            amount=Amount(value=Decimal("0.1"), asset="BTC"),
            fee=Decimal("0.04"),
            cost=Decimal("11"),
            executed_at=Timestamp(dt=base + timedelta(minutes=2)),
        )
    )


async def _seed_news(
    storage: SQLiteStorageAdapter,
    *,
    headline: str = "BTC moves",
    coins: list[str] | None = None,
    minutes_ago: int = 5,
    source: str = "rss:test",
    external_id: str = "abc",
) -> None:
    now = datetime.now(UTC)
    # Explicit None check — an empty list is a meaningful "no coins mentioned"
    # value distinct from "default to BTC".
    resolved_coins = ["BTC"] if coins is None else coins
    await storage.save_news_item(
        NewsItem(
            source=source,
            external_id=external_id,
            published_at=Timestamp(dt=now - timedelta(minutes=minutes_ago)),
            headline=headline,
            mentioned_coins=resolved_coins,
        )
    )


class TestMetricsPath:
    async def test_minimum_summary_with_prices_only(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        builder = SummaryBuilder(storage)
        summary = await builder.build(BTC_USD, lookback=timedelta(hours=1))

        assert summary.symbol == "BTC/USD"
        assert summary.snapshot_count == 5
        assert summary.latest_price == 108.0
        assert summary.volatility > 0
        assert summary.max_drawdown < 0
        assert 0 <= summary.flatness <= 1
        assert summary.cycle_count == 0
        assert summary.recent_news == []
        # Without supplied grid, fields are all None
        assert summary.current_grid.spacing_percentage is None

    async def test_supplied_grid_carries_through(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        builder = SummaryBuilder(storage)
        grid = CurrentGridParams(
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        )
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            current_grid=grid,
            active_orders=6,
        )
        assert summary.current_grid.spacing_percentage == 1.0
        assert summary.current_grid.levels_above == 3
        assert summary.active_orders == 6

    async def test_no_data_yields_safe_defaults(self, storage: SQLiteStorageAdapter) -> None:
        builder = SummaryBuilder(storage)
        summary = await builder.build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.snapshot_count == 0
        assert summary.latest_price is None
        assert summary.volatility == 0.0
        assert summary.max_drawdown == 0.0
        assert summary.flatness == 1.0  # vacuously "flat"
        assert summary.cycle_count == 0

    async def test_cycle_stats_computed_from_trades(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        await _seed_trades(storage)
        builder = SummaryBuilder(storage)
        summary = await builder.build(BTC_USD, lookback=timedelta(hours=1))
        # One profitable buy@100 → sell@110 cycle, fees 0.04 each leg
        assert summary.cycle_count == 1
        assert summary.win_rate == 1.0
        # PnL = 11 - 10 - 0.04 - 0.04 = 0.92
        assert summary.total_pnl == pytest.approx(0.92)


class TestNewsPath:
    async def test_news_omitted_when_lookback_none(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        await _seed_news(storage)
        builder = SummaryBuilder(storage)
        summary = await builder.build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.recent_news == []

    async def test_news_included_when_lookback_set(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        await _seed_news(storage, headline="Bitcoin rallies")
        builder = SummaryBuilder(storage)
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            news_lookback=timedelta(hours=1),
        )
        assert len(summary.recent_news) == 1
        item = summary.recent_news[0]
        assert item.headline == "Bitcoin rallies"
        assert item.mentioned_coins == ["BTC"]
        # NewsItemSummary drops body and fetched_at — has only the
        # advisor-relevant fields:
        assert hasattr(item, "source")
        assert hasattr(item, "published_at")
        assert hasattr(item, "sentiment_score")

    async def test_news_match_coin_filter(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_news(storage, headline="BTC story", coins=["BTC"], external_id="1")
        await _seed_news(storage, headline="ETH story", coins=["ETH"], external_id="2")
        await _seed_news(storage, headline="Macro", coins=[], external_id="3")
        builder = SummaryBuilder(storage)
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            news_lookback=timedelta(hours=1),
            news_match_coin=True,
        )
        # Only the BTC item passes the filter
        assert [item.headline for item in summary.recent_news] == ["BTC story"]

    async def test_news_limit_respected(self, storage: SQLiteStorageAdapter) -> None:
        for i in range(10):
            await _seed_news(storage, external_id=f"e{i}", minutes_ago=i)
        builder = SummaryBuilder(storage)
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            news_lookback=timedelta(hours=1),
            news_limit=3,
        )
        assert len(summary.recent_news) == 3

    async def test_news_window_excludes_old_items(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_news(storage, external_id="recent", minutes_ago=5)
        await _seed_news(storage, external_id="old", minutes_ago=120)
        builder = SummaryBuilder(storage)
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            news_lookback=timedelta(minutes=30),
        )
        assert len(summary.recent_news) == 1

    async def test_news_sentiment_passes_through(self, storage: SQLiteStorageAdapter) -> None:
        now = datetime.now(UTC)
        await storage.save_news_item(
            NewsItem(
                source="rss:test",
                external_id="sentiment-1",
                published_at=Timestamp(dt=now - timedelta(minutes=5)),
                headline="Mixed news",
                sentiment_score=0.3,
                mentioned_coins=["BTC"],
            )
        )
        builder = SummaryBuilder(storage)
        summary = await builder.build(
            BTC_USD,
            lookback=timedelta(hours=1),
            news_lookback=timedelta(hours=1),
        )
        assert summary.recent_news[0].sentiment_score == 0.3

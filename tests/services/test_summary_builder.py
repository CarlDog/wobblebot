"""Unit tests for SummaryBuilder (Stage 3.3 Slice B)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.models import NewsItem, Trade
from wobblebot.domain.value_objects import (
    Amount,
    OHLCBar,
    OrderSide,
    Price,
    Symbol,
    Timestamp,
)
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary
from wobblebot.services.summary_builder import _TA_FIELD_NAMES, SummaryBuilder

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


def _make_ta_bar(opened_at: datetime, close: float) -> OHLCBar:
    return OHLCBar(
        symbol=BTC_USD,
        interval_minutes=60,
        opened_at=opened_at,
        open=Decimal(str(close)),
        high=Decimal(str(close + 2)),
        low=Decimal(str(close - 2)),
        close=Decimal(str(close)),
        vwap=Decimal("0"),
        volume=Decimal("1"),
        count=1,
    )


async def _seed_hourly_bars(
    storage: SQLiteStorageAdapter, *, count: int, newest_age_hours: float = 0.5
) -> None:
    """``count`` hourly bars, newest opening ``newest_age_hours`` ago."""
    now = datetime.now(UTC)
    bars = [
        _make_ta_bar(
            now - timedelta(hours=newest_age_hours + (count - 1 - i)),
            100.0 + (i % 9),
        )
        for i in range(count)
    ]
    await storage.save_ohlc_bars(bars)


class TestTAFields:
    """P2 slice 3 — the 16 TA indicator fields on PerformanceSummary."""

    async def test_fresh_bars_populate_indicators(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        await _seed_hourly_bars(storage, count=250)
        summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is not None
        assert summary.macd_line is not None
        assert summary.macd_histogram == pytest.approx(
            summary.macd_line - summary.macd_signal  # type: ignore[operator]
        )
        assert summary.bollinger_middle == pytest.approx(summary.sma_20)  # type: ignore[arg-type]
        assert summary.sma_200 is not None
        assert summary.atr_14 is not None
        assert summary.adx_14 is not None
        assert summary.stochastic_k is not None

    async def test_ta_field_shape_pinned_three_ways(self, storage: SQLiteStorageAdapter) -> None:
        """The 16-field shape lives in three places — ``_TA_FIELD_NAMES``,
        the builder's healthy-path dict literal, and PerformanceSummary's
        declarations — and PerformanceSummary ignores extra keys, so a
        typo'd dict key would silently ship the real field as ``None``
        (indistinguishable from legitimate no-TA). Pin all three."""
        assert set(_TA_FIELD_NAMES) <= set(PerformanceSummary.model_fields)
        await _seed_hourly_bars(storage, count=250)
        fields = await SummaryBuilder(
            storage
        )._compute_ta_fields(  # pylint: disable=protected-access
            BTC_USD, now=datetime.now(UTC), interval_minutes=60
        )
        assert set(fields) == set(_TA_FIELD_NAMES)

    async def test_no_bars_yields_all_none(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is None
        assert summary.macd_line is None
        assert summary.sma_200 is None
        assert summary.stochastic_d is None

    async def test_stale_bars_yield_all_none_with_warning(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bars exist but the newest opened 12h ago (> 3 intervals):
        stale indicators presented as current would poison the regime
        read, so every TA field must go out None — and loudly."""
        await _seed_prices(storage)
        await _seed_hourly_bars(storage, count=250, newest_age_hours=12.0)
        with caplog.at_level(logging.WARNING, logger="wobblebot.services.summary_builder"):
            summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is None
        assert summary.adx_14 is None
        assert any("TA fields null" in r.getMessage() for r in caplog.records)

    async def test_short_fresh_window_partial_none(self, storage: SQLiteStorageAdapter) -> None:
        """40 fresh bars: RSI(14)/ATR(14) compute; SMA(200) can't —
        per-indicator None, not all-or-nothing."""
        await _seed_prices(storage)
        await _seed_hourly_bars(storage, count=40)
        summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is not None
        assert summary.atr_14 is not None
        assert summary.sma_200 is None

    async def test_other_interval_bars_do_not_feed_ta(self, storage: SQLiteStorageAdapter) -> None:
        """1m bars must not satisfy the 60m TA read."""
        await _seed_prices(storage)
        now = datetime.now(UTC)
        minute_bars = [
            OHLCBar(
                symbol=BTC_USD,
                interval_minutes=1,
                opened_at=now - timedelta(minutes=i),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                vwap=Decimal("0"),
                volume=Decimal("1"),
                count=1,
            )
            for i in range(30)
        ]
        await storage.save_ohlc_bars(minute_bars)
        summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is None

    async def test_months_old_bars_still_warn_not_silence(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bars older than the whole 260-bar fetch window (e.g. a dump
        import that ended a quarter ago) fall outside the windowed read
        — the guard must check the cursor and still WARN, not demote
        the stalest data of all to a silent DEBUG. Caught live
        2026-08-08."""
        await _seed_prices(storage)
        await _seed_hourly_bars(storage, count=50, newest_age_hours=24 * 90)
        with caplog.at_level(logging.WARNING, logger="wobblebot.services.summary_builder"):
            summary = await SummaryBuilder(storage).build(BTC_USD, lookback=timedelta(hours=1))
        assert summary.rsi_14 is None
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "TA fields null" in rendered
        assert "--resume" in rendered

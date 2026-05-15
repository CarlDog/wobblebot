"""SummaryBuilder — Stage 3.3 service that composes advisor input from storage.

Reads Stage 3.1 price snapshots + Stage 3.2.5 news items + the
operator-supplied current grid config, computes derived metrics,
and emits a ``PerformanceSummary`` ready to feed to ``AdvisorPort``.

**Why not DataCollector?** ``DataCollector`` is the engine's
metrics surface and requires an ``ExchangePort`` for live reads
(price, balances). ``cli/advise`` runs decoupled from the engine
and only needs the historical reads, so a dedicated builder over
``StoragePort`` alone is the right shape.

**News filtering.** When ``news_match_coin=True`` the builder
restricts ``recent_news`` to items whose ``mentioned_coins`` contains
the symbol's base. Off by default — macro / regulatory / exchange-
outage news is symbol-agnostic but still relevant, so the default is
"all news the operator's poller has collected."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.advisor import (
    CurrentGridParams,
    NewsItemSummary,
    PerformanceSummary,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.services.metrics import (
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)


class SummaryBuilder:
    """Composes a ``PerformanceSummary`` from persisted history.

    Args:
        storage: The ``StoragePort`` holding price snapshots, trades,
            and news items.
    """

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    async def build(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        symbol: Symbol,
        *,
        lookback: timedelta,
        news_lookback: timedelta | None = None,
        news_limit: int = 20,
        news_match_coin: bool = False,
        current_grid: CurrentGridParams | None = None,
        active_orders: int = 0,
    ) -> PerformanceSummary:
        """Build a ``PerformanceSummary`` for one symbol over ``lookback``.

        Args:
            symbol: Trading pair to summarize.
            lookback: Window for price metrics + cycle stats.
            news_lookback: Window for news items. ``None`` means no
                news context — useful when the operator hasn't run
                ``cli/news`` or wants metrics-only summaries.
            news_limit: Maximum number of news items to include.
            news_match_coin: When True, only include news items whose
                ``mentioned_coins`` contains the symbol's base.
            current_grid: Operator-supplied snapshot of the engine's
                current grid params. When ``None``, the summary
                reports empty grid params (advisor sees "unconfigured").
            active_orders: Count of orders currently on the book.

        Returns:
            A populated ``PerformanceSummary`` ready for
            ``AdvisorPort.get_recommendation``.
        """
        now = datetime.now(UTC)
        start_time = now - lookback

        snapshots = await self._storage.get_price_snapshots(symbol=symbol, start_time=start_time)
        trades_desc = await self._storage.get_trades(
            symbol=symbol, start_time=start_time, limit=10000
        )
        trades_asc = list(reversed(trades_desc))
        prices = [s.price.amount for s in snapshots]

        recent_news = await self._build_news_summaries(
            symbol=symbol,
            news_lookback=news_lookback,
            news_limit=news_limit,
            news_match_coin=news_match_coin,
        )

        cycle = compute_cycle_stats(trades_asc)
        latest_price = float(snapshots[-1].price.amount) if snapshots else None

        return PerformanceSummary(
            symbol=str(symbol),
            lookback_hours=lookback.total_seconds() / 3600,
            latest_price=latest_price,
            snapshot_count=len(snapshots),
            volatility=float(compute_volatility(prices)) if prices else 0.0,
            max_drawdown=float(compute_max_drawdown(prices)) if prices else 0.0,
            flatness=float(compute_flatness(prices)) if prices else 1.0,
            cycle_count=cycle.cycle_count,
            win_rate=_decimal_to_float(cycle.win_rate),
            total_pnl=_decimal_to_float(cycle.total_pnl),
            active_orders=active_orders,
            current_grid=current_grid or CurrentGridParams(),
            recent_news=recent_news,
        )

    async def _build_news_summaries(
        self,
        *,
        symbol: Symbol,
        news_lookback: timedelta | None,
        news_limit: int,
        news_match_coin: bool,
    ) -> list[NewsItemSummary]:
        if news_lookback is None:
            return []
        since = datetime.now(UTC) - news_lookback
        items = await self._storage.get_news_items(since=since, limit=news_limit)
        result: list[NewsItemSummary] = []
        for item in items:
            if news_match_coin and symbol.base not in item.mentioned_coins:
                continue
            result.append(
                NewsItemSummary(
                    source=item.source,
                    published_at=item.published_at,
                    headline=item.headline,
                    sentiment_score=item.sentiment_score,
                    mentioned_coins=list(item.mentioned_coins),
                )
            )
        return result


def _decimal_to_float(value: Decimal) -> float:
    """Decimal → float for the LLM-facing JSON wire format."""
    return float(value)

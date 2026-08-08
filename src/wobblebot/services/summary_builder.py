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

import logging
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
from wobblebot.services.ta_metrics import (
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_sma,
    compute_stochastic,
)

_LOGGER = logging.getLogger("wobblebot.services.summary_builder")

# TA bar window: SMA(200) is the heaviest consumer; 260 bars gives it
# headroom plus full ADX/MACD warmup. At the 60m default interval this
# is ~11 days of history.
_TA_LOOKBACK_BARS = 260

# Newest bar must open within this many intervals of "now" or the TA
# fields go out as None — stale indicators presented as current would
# quietly poison the advisor's regime read (ADR-019: trend dominates).
_TA_MAX_STALENESS_INTERVALS = 3

_TA_FIELD_NAMES = (
    "rsi_14",
    "macd_line",
    "macd_signal",
    "macd_histogram",
    "bollinger_upper",
    "bollinger_middle",
    "bollinger_lower",
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_12",
    "ema_26",
    "atr_14",
    "adx_14",
    "stochastic_k",
    "stochastic_d",
)


class SummaryBuilder:
    """Composes a ``PerformanceSummary`` from persisted history.

    Args:
        storage: The ``StoragePort`` holding price snapshots and
            trades. In production this is the live/shadow/observe DB,
            depending on which the operator wants the advisor to
            reason about.
        news_storage: Optional separate ``StoragePort`` for news
            items. Project convention keeps observe / news / live in
            separate SQLite files, so the news adapter must point at
            the news DB. Defaults to ``storage`` when ``None`` —
            useful for tests and for setups where everything lives
            in one DB.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        news_storage: StoragePort | None = None,
    ) -> None:
        self._storage = storage
        self._news_storage = news_storage if news_storage is not None else storage

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
        ta_interval_minutes: int = 60,
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
            ta_interval_minutes: Bar interval the TA indicators read
                (P2 slice 3). Default 60 — the spine's standard. TA
                fields come back ``None`` when bars are absent, too
                short, or stale (see the staleness guard).

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
        ta_fields = await self._compute_ta_fields(
            symbol, now=now, interval_minutes=ta_interval_minutes
        )

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
            **ta_fields,
        )

    async def _compute_ta_fields(
        self,
        symbol: Symbol,
        *,
        now: datetime,
        interval_minutes: int,
    ) -> dict[str, float | None]:
        """The 16 TA indicator fields, or all-``None`` when bars can't back them.

        Reads the newest ``_TA_LOOKBACK_BARS`` window of ``ohlc_bars``
        at ``interval_minutes``. All-``None`` cases:

        - no bars at that interval (DEBUG — normal before the operator
          imports/backfills bar history), or
        - newest bar older than ``_TA_MAX_STALENESS_INTERVALS``
          intervals (WARN — bar history exists but nothing is keeping
          it fresh; stale indicators presented as current would poison
          the regime read, so we refuse instead).

        Individual indicators may still be ``None`` inside a fresh
        window when it's too short for their period (e.g. sma_200
        needs 200 bars; a new listing may only have 40).
        """
        empty: dict[str, float | None] = dict.fromkeys(_TA_FIELD_NAMES)
        window = timedelta(minutes=interval_minutes * _TA_LOOKBACK_BARS)
        bars = await self._storage.get_ohlc_bars(symbol, interval_minutes, start_time=now - window)
        if not bars:
            _LOGGER.debug(
                "no %dm bars for %s — TA fields null",
                interval_minutes,
                symbol,
                extra={"symbol": str(symbol), "interval_minutes": interval_minutes},
            )
            return empty
        staleness = now - bars[-1].opened_at
        max_staleness = timedelta(minutes=interval_minutes * _TA_MAX_STALENESS_INTERVALS)
        if staleness > max_staleness:
            _LOGGER.warning(
                "newest %dm bar for %s is %.1fh old — TA fields null; "
                "top up with `cli/observe --backfill --resume --intervals %dm` "
                "or the history import",
                interval_minutes,
                symbol,
                staleness.total_seconds() / 3600.0,
                interval_minutes,
                extra={
                    "symbol": str(symbol),
                    "interval_minutes": interval_minutes,
                    "newest_opened_at": bars[-1].opened_at.isoformat(),
                    "staleness_hours": round(staleness.total_seconds() / 3600.0, 1),
                },
            )
            return empty
        macd = compute_macd(bars)
        bollinger = compute_bollinger(bars)
        stochastic = compute_stochastic(bars)
        return {
            "rsi_14": compute_rsi(bars),
            "macd_line": macd.line if macd else None,
            "macd_signal": macd.signal if macd else None,
            "macd_histogram": macd.histogram if macd else None,
            "bollinger_upper": bollinger.upper if bollinger else None,
            "bollinger_middle": bollinger.middle if bollinger else None,
            "bollinger_lower": bollinger.lower if bollinger else None,
            "sma_20": compute_sma(bars, 20),
            "sma_50": compute_sma(bars, 50),
            "sma_200": compute_sma(bars, 200),
            "ema_12": compute_ema(bars, 12),
            "ema_26": compute_ema(bars, 26),
            "atr_14": compute_atr(bars),
            "adx_14": compute_adx(bars),
            "stochastic_k": stochastic.k if stochastic else None,
            "stochastic_d": stochastic.d if stochastic else None,
        }

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
        items = await self._news_storage.get_news_items(since=since, limit=news_limit)
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

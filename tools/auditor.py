"""Historical-replay auditor — "what would my config have done?" (ADR-028, P2 slice 4).

Replays the operator's ``settings.yml`` grid + safety config through the
REAL ``GridEngine`` over stored historical bars and reports fills, fees,
realized cycle PnL, mark-to-market net PnL, max drawdown, and
cycle-completion rate per symbol. Pure historical replay: no orders
placed anywhere real, no Kraken calls — bars come from the observe DB
(``tools/import_kraken_history.py`` / ``cli/observe --backfill`` put
them there).

The three ADR-028 judge corrections, honored here:

1. ``max_daily_spend_usd`` is NEUTERED for the replay (set to an
   absurdly large value via ``model_copy``). The engine's daily-spend
   window is wall-clock (`datetime.now``), so a multi-day replay would
   exhaust the cap in its first wall-clock "day" and silently refuse
   every later BUY — a near-zero-activity result that looks like "your
   config is ultra-conservative." The bar-time alternative is a trap:
   replayed orders persist with wall-clock ``created_at``, so a
   bar-time window would match EVERY order ever placed. All other caps
   (exposure, per-coin, order-count) plus the ADR-032 sell guard stay
   exactly as configured — they are part of the config being audited.
2. ``AuditorExchangeAdapter`` suppresses the mock's fill-at-placement:
   the inherited ``place_order`` immediately matches against the
   current price, which would fill a counter in the SAME bar whenever
   ``close`` already crosses it — over-counting cycles and fees
   (negligible at 1m, material at 1h/4h). Orders placed during a bar
   become fillable from the NEXT price step, matching ``_Sim``.
3. The anchor warm-starts at bar-0 ``open`` by construction: each bar
   is replayed as the price sequence ``open -> low -> high -> close``,
   so the first price the engine ever sees — the one ``_initialize``
   anchors to — is the first bar's open (the reference ``_Sim``'s
   anchor).

Honest caveat (ADR-028): the audit is DIRECTIONAL, not exact — it
ranks configs and surfaces trends. ``_Sim``-equivalence holds at 1m
granularity only; coarser bars lose intra-bar ordering.

Usage::

    python tools/auditor.py --symbols BTC/USD --days 30
    python tools/auditor.py --symbols BTC/USD,ETH/USD --since 2026-01-01 --until 2026-03-31
    python tools/auditor.py --symbols BTC/USD --days 90 --interval 1h --seed-usd 250
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    load_operator_env,
    parse_date_arg,
    parse_days_arg,
    parse_interval_arg,
    parse_symbol_csv,
)
from wobblebot.config.grid import (
    KRAKEN_MAKER_FEE_RATE,
    KRAKEN_TAKER_FEE_RATE,
    GridConfig,
)
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.metrics import compute_cycle_stats, compute_max_drawdown

_LOGGER = logging.getLogger("wobblebot.tools.auditor")

_DEFAULT_DB = Path("data") / "wobblebot-observe.db"

# Correction 1: effectively-unlimited daily spend for the replay. The
# field validates gt=0, so "neutered" is a huge Decimal, never zero.
# Public: the P4.2 evaluator (tools/score_recommendations.py) neuters
# BOTH of its replay arms with this same value (ADR-035).
NEUTERED_DAILY_SPEND = Decimal("1000000000")

# Progress cadence for long 1m replays (4 engine steps per bar).
_PROGRESS_EVERY_BARS = 10_000


class AuditorExchangeAdapter(MockExchangeAdapter):
    """``MockExchangeAdapter`` minus the fill-at-placement (correction 2).

    The inherited ``place_order`` calls ``_match_open_orders`` inline so
    a just-placed order can fill against the current price. During a
    replay that lets a counter fill in the same bar that placed it.
    This subclass defers matching while inside ``place_order``; the
    order becomes fillable from the next ``set_price`` step. Everything
    else (balance checks, fees, trade history) is inherited unchanged.
    """

    def __init__(
        self,
        starting_balances: dict[str, Decimal] | None = None,
        starting_prices: dict[Symbol, Decimal] | None = None,
        fee_rate: Decimal | None = None,
    ) -> None:
        # Mirrors the parent's keyword signature so mypy checks call
        # sites; fee_rate=None defers to the parent's default rather
        # than duplicating its private constant here.
        if fee_rate is None:
            super().__init__(starting_balances=starting_balances, starting_prices=starting_prices)
        else:
            super().__init__(
                starting_balances=starting_balances,
                starting_prices=starting_prices,
                fee_rate=fee_rate,
            )
        self._in_place_order = False

    async def place_order(self, order: Order) -> Order:
        self._in_place_order = True
        try:
            return await super().place_order(order)
        finally:
            self._in_place_order = False

    def _match_open_orders(self, symbol: Symbol) -> list[Trade]:
        if self._in_place_order:
            return []
        return super()._match_open_orders(symbol)


@dataclass(frozen=True)
class SymbolAuditResult:  # pylint: disable=too-many-instance-attributes
    """One symbol's replay outcome. Every field is a report line."""

    symbol: Symbol
    interval_minutes: int
    bars_replayed: int
    first_bar: datetime
    last_bar: datetime
    buys: int
    sells: int
    fees: Decimal
    cycle_count: int
    win_rate: Decimal
    realized_pnl: Decimal
    seed_usd: Decimal
    final_portfolio_usd: Decimal
    net_pnl_usd: Decimal
    max_drawdown: Decimal
    refusals: int
    sells_deferred: int


async def replay_symbol(  # pylint: disable=too-many-locals,too-many-arguments
    bars: list[OHLCBar],
    symbol: Symbol,
    *,
    grid_config: GridConfig,
    safety_config: SafetyConfig,
    seed_usd: Decimal,
    seed_base: Decimal,
    fee_rate: Decimal,
    maker_fee_rate: Decimal = KRAKEN_MAKER_FEE_RATE,
    taker_fee_rate: Decimal = KRAKEN_TAKER_FEE_RATE,
    replay_storage: SQLiteStorageAdapter | None = None,
) -> SymbolAuditResult:
    """Drive one symbol's bars through a fresh engine + adapter.

    ``replay_storage`` is the test seam — when provided the caller owns
    its lifecycle (and can inspect replayed orders/trades afterwards);
    when ``None`` a throwaway ``:memory:`` store is used.

    ``maker_fee_rate`` / ``taker_fee_rate`` feed the ENGINE (the ADR-032
    sell guard's profitability check); ``fee_rate`` is what the exchange
    adapter charges per fill. Callers replaying a historical window
    should pass the schedule in force DURING that window for all three,
    or the guard reasons about fees the fills aren't paying (the P4.2
    evaluator does this; Kraken doubled the schedule 2026-07-09).
    """
    adapter = AuditorExchangeAdapter(
        starting_balances={symbol.quote: seed_usd, symbol.base: seed_base},
        starting_prices={symbol: bars[0].open},
        fee_rate=fee_rate,
    )
    owns_storage = replay_storage is None
    storage = replay_storage if replay_storage is not None else SQLiteStorageAdapter(":memory:")
    if owns_storage:
        await storage.connect()
    started = time.monotonic()
    refusals = 0
    sells_deferred = 0
    portfolio_values: list[Decimal] = []
    try:
        engine = GridEngine(
            adapter,
            storage,
            grid_config,
            safety_config,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
        )
        for index, ohlc_bar in enumerate(bars):
            # Correction 3 by construction: bar-0 open is the first
            # price the engine sees, so _initialize anchors there.
            for price in (ohlc_bar.open, ohlc_bar.low, ohlc_bar.high, ohlc_bar.close):
                adapter.set_price(symbol, price)
                step = await engine.step(symbol)
                refusals += step.refusals
                sells_deferred += step.sells_deferred
            portfolio_values.append(await _portfolio_usd(adapter, symbol, ohlc_bar.close))
            if (index + 1) % _PROGRESS_EVERY_BARS == 0:
                _LOGGER.info(
                    "audit %s: %d/%d bars, %.1fs elapsed",
                    symbol,
                    index + 1,
                    len(bars),
                    time.monotonic() - started,
                )
        trades_desc = await storage.get_trades(symbol=symbol, limit=1_000_000)
        trades_asc = list(reversed(trades_desc))
        cycle = compute_cycle_stats(trades_asc)
        buys = sum(1 for t in trades_asc if t.side.value == "buy")
        sells = len(trades_asc) - buys
        final_value = portfolio_values[-1] if portfolio_values else seed_usd
        return SymbolAuditResult(
            symbol=symbol,
            interval_minutes=bars[0].interval_minutes,
            bars_replayed=len(bars),
            first_bar=bars[0].opened_at,
            last_bar=bars[-1].opened_at,
            buys=buys,
            sells=sells,
            fees=sum((t.fee for t in trades_asc), Decimal("0")),
            cycle_count=cycle.cycle_count,
            win_rate=cycle.win_rate,
            realized_pnl=cycle.total_pnl,
            seed_usd=seed_usd,
            final_portfolio_usd=final_value,
            net_pnl_usd=final_value - seed_usd - seed_base * bars[0].open,
            max_drawdown=compute_max_drawdown(portfolio_values),
            refusals=refusals,
            sells_deferred=sells_deferred,
        )
    finally:
        if owns_storage:
            await storage.close()


async def _portfolio_usd(
    adapter: MockExchangeAdapter, symbol: Symbol, mark_price: Decimal
) -> Decimal:
    """Total quote + base holdings marked at ``mark_price``."""
    quote_balance = await adapter.get_balance(symbol.quote)
    base_balance = await adapter.get_balance(symbol.base)
    quote_total = quote_balance.total if quote_balance else Decimal("0")
    base_total = base_balance.total if base_balance else Decimal("0")
    return quote_total + base_total * mark_price


def _render_report(result: SymbolAuditResult) -> None:
    """Operator-facing report lines (message-first per logging convention)."""
    _LOGGER.info(
        "audit %s @ %dm: %d bars, %s -> %s",
        result.symbol,
        result.interval_minutes,
        result.bars_replayed,
        result.first_bar.date().isoformat(),
        result.last_bar.date().isoformat(),
    )
    completion = Decimal(result.cycle_count) / Decimal(result.buys) if result.buys else Decimal("0")
    _LOGGER.info(
        "  fills: %d buys / %d sells; fees $%.2f; cycles %d "
        "(completion %.0f%%, win rate %.0f%%); realized PnL $%.2f",
        result.buys,
        result.sells,
        result.fees,
        result.cycle_count,
        completion * 100,
        result.win_rate * 100,
        result.realized_pnl,
    )
    _LOGGER.info(
        "  portfolio: seed $%.2f -> final $%.2f (net $%+.2f); max drawdown %.1f%%; "
        "cap refusals %d; sells deferred %d",
        result.seed_usd,
        result.final_portfolio_usd,
        result.net_pnl_usd,
        result.max_drawdown * 100,
        result.refusals,
        result.sells_deferred,
    )
    if result.interval_minutes > 1:
        _LOGGER.info(
            "  caveat: %dm bars lose intra-bar ordering -- directional only; "
            "the audit is _Sim-equivalent at 1m (import 1m history for precision)",
            result.interval_minutes,
        )


async def _run(
    args: argparse.Namespace,
) -> int:  # pylint: disable=too-many-locals,too-many-return-statements
    try:
        config = load_resolved_config(config_path=args.config, profile_name=args.profile)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        _LOGGER.error("config load failed: %s", exc)
        return 2

    if args.days is not None:
        since = datetime.now(UTC) - timedelta(days=args.days)
    elif args.since is not None:
        since = args.since
    else:
        _LOGGER.error("--since or --days is required")
        return 2
    until = args.until if args.until is not None else datetime.now(UTC)
    if since >= until:
        _LOGGER.error("--since must be strictly before --until")
        return 2

    symbols = [Symbol.from_string(s) for s in parse_symbol_csv(args.symbols)]
    if not symbols:
        _LOGGER.error("--symbols resolved to an empty list")
        return 2

    # Correction 1: neuter ONLY the wall-clock-poisoned daily cap.
    safety_config = config.safety.model_copy(update={"max_daily_spend_usd": NEUTERED_DAILY_SPEND})
    _LOGGER.info(
        "replaying with max_daily_spend_usd neutered (wall-clock window is "
        "meaningless in replay); all other caps + sell guard as configured"
    )

    bar_storage = SQLiteStorageAdapter(str(args.db))
    await bar_storage.connect()
    try:
        any_error = False
        for symbol in symbols:
            bars = await bar_storage.get_ohlc_bars(
                symbol, args.interval, start_time=since, end_time=until
            )
            if not bars:
                _LOGGER.error(
                    "no %dm bars for %s in %s -> %s; import them first "
                    "(tools/import_kraken_history.py --symbols %s --intervals %dm)",
                    args.interval,
                    symbol,
                    since.date().isoformat(),
                    until.date().isoformat(),
                    symbol,
                    args.interval,
                )
                any_error = True
                continue
            result = await replay_symbol(
                bars,
                symbol,
                grid_config=config.grid,
                safety_config=safety_config,
                seed_usd=args.seed_usd,
                seed_base=args.seed_base,
                fee_rate=args.fee_rate,
            )
            _render_report(result)
        return 1 if any_error else 0
    finally:
        await bar_storage.close()


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbols", required=True, help="Comma-separated pairs to audit (e.g. BTC/USD)."
    )
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", type=parse_date_arg, default=None)
    window.add_argument("--days", type=parse_days_arg, default=None)
    parser.add_argument("--until", type=parse_date_arg, default=None)
    parser.add_argument(
        "--interval",
        type=parse_interval_arg,
        default=1,
        help="Bar interval to replay. Default 1m -- the _Sim-equivalent regime.",
    )
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="Observe DB holding the bars.")
    parser.add_argument(
        "--seed-usd",
        type=Decimal,
        default=Decimal("10000"),
        help="Starting quote balance. Set to your real balance for honest caps.",
    )
    parser.add_argument(
        "--seed-base",
        type=Decimal,
        default=Decimal("0"),
        help="Starting base-asset balance (0 = SELL levels refuse until buys fill).",
    )
    parser.add_argument(
        "--fee-rate",
        type=Decimal,
        default=KRAKEN_MAKER_FEE_RATE,
        help=(
            "Flat fee fraction per fill (default: the current Kraken "
            "Tier-1 maker constant — 0.40%% since the 2026-07-09 doubling; "
            "the pre-ADR-038 default here was the retired 0.26%%)."
        ),
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

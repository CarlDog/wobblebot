"""Status dashboard — reads live.db's open orders + recent fills (Stage 7.2.B).

The status surface is the operator's at-a-glance "what is cli/live
doing right now". Two cards:

- **Open orders** — `StoragePort.get_open_orders()` against live.db.
- **Recent fills** — `StoragePort.get_trades(limit=20)` against the
  same DB.

Both gracefully degrade when ``live_storage`` is ``None`` (the four
optional cross-DB paths in ``WebConfig`` per Stage 5.6.C's pattern)
— the cards render with an "unwired" placeholder so the absence is
visible to the operator rather than silently hiding the section.

Per ADR-016 routes consume the existing ports; no engine state +
no money mutations here. Mutations live in
``wobblebot.web.routes.commands`` (Stage 7.2.C).

**Re-anchor banners annotate, they never suppress.** Both viability
stats (``recent_range_spacings``, ``atr_spacings_per_hour``) can say
"a re-anchored grid probably wouldn't cycle here" — and neither one
hides or downgrades a banner when it does. Two reasons. First,
"re-anchoring isn't worth it" and "the current grid is fine" are
different states: a misplaced ladder in a dead market is still
capital sitting idle, and the right response may be *pause this
symbol*, not *leave it drifted and say nothing*. Second, per the
advisor philosophy (ADR-002, transparent guardrail), a heuristic that
silently withholds information the operator would have acted on is
exactly the failure mode this project designs against. Show the
number; let the operator weigh it. The execution-feedback annotation
(a recent re-anchor's result tally, 2026-08-17) follows the same rule
— see the ``status_reanchor`` module docstring for the full argument.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse, Response

from wobblebot.config.cli import ReanchorSeverity
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.models import Balance, CapTripRecord, Order, Trade
from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Symbol, fmt_qty, fmt_usd
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cool_down import check_cool_down
from wobblebot.services.cycle_matcher import RecentCycle, match_cycles, today_realized_pnl
from wobblebot.services.staking_income import income_by_asset, value_income_usd
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_cool_down_minutes,
    get_live_storage,
    get_live_tick_seconds,
    get_observe_storage,
    get_operator_storage,
    get_reanchor_min_severity,
    get_templates,
)
from wobblebot.web.routes.status_reanchor import (
    ReanchorRecommendation,
    load_reanchor_recommendations,
    load_reanchor_snoozes,
)

_LOGGER = logging.getLogger(__name__)
# ADR-030 freshness guard: an engine_state row counts as CURRENT only
# when written within this many live-engine ticks. At the 5s default
# tick, 3 ticks = 15s ≈ one htmx poll interval — a dead engine's rows
# age out of the badge layer within one dashboard refresh.
_ENGINE_STATE_FRESHNESS_TICKS = 3
# Fallback when cli/web has no live: section to read tick_seconds
# from — matches LiveConfig's schema default.
_DEFAULT_TICK_SECONDS = 5.0
# Window used both for the latest-price fetch AND the trend
# baseline (oldest snapshot in the window). 15 min is short enough
# to feel live, long enough to smooth per-tick noise.
_PRICE_LOOKBACK_MINUTES = 15
# Percent change below this threshold renders as "flat" (no arrow).
# Without a threshold the arrow flickers up/down on stable markets.
_TREND_FLAT_THRESHOLD = Decimal("0.001")  # 0.1%
# Trade-fetch window. Wide enough to match cycles over the bot's full
# history (lifetime PnL + correct FIFO pairing) at this capital's volume;
# mirrors the cost page's all-time fee aggregation. Revisit if the trade
# count ever approaches this.
_TRADE_FETCH_LIMIT = 10_000
# Ledger rows are a few hundred lifetime; this covers the whole table
# so the income total is never computed from a truncated read.
_LEDGER_FETCH_LIMIT = 100_000
# Cap on cycles rendered in the Recent Cycles table (the full set still
# feeds the lifetime-PnL aggregate). Keeps the table bounded once the
# match window covers the whole history.
_RECENT_CYCLES_DISPLAY = 10
# Cap on fills rendered in the Recent Fills table — symmetric with cycles.
_RECENT_FILLS_DISPLAY = 10
# Per-symbol sparkline geometry (a tiny inline SVG on each symbol card:
# recent price line + grid band + current-price marker). Viewbox units.
_SPARK_W = 100.0
_SPARK_H = 24.0
_SPARK_PAD = 2.0
_SPARK_LOOKBACK_MINUTES = 120  # 2h of price snapshots

TrendDirection = Literal["up", "down", "flat"]

router = APIRouter(tags=["status"])


@dataclass(frozen=True)
class Sparkline:
    """Pre-computed SVG geometry for one symbol's price sparkline.

    The template plugs these straight into an ``<svg viewBox="0 0 100 24">``
    — a polyline of the recent price series, an optional shaded band
    spanning the open-order ladder (lowest BUY → highest SELL), and a
    marker at the current price. ``offside`` flags the marker red when
    price has left the band (the visual "parked" signal).
    """

    points: str  # polyline "x,y x,y ..."
    band_y: float | None  # top of the band rect (None = no open orders)
    band_h: float | None
    marker_x: float
    marker_y: float
    offside: bool


@dataclass(frozen=True)
class HeldInventory:
    """How much of one asset the operator holds, and what it's worth.

    ``value_usd`` is ``None`` when no observed price is available —
    the position is real either way, so the card shows the amount and
    omits the valuation rather than hiding the holding.
    """

    amount: Decimal
    value_usd: Decimal | None


@dataclass(frozen=True)
class FillsSummary:
    """Aggregate stats for the fills currently on screen.

    Scoped to exactly the rows in ``recent_trades`` — the subhead has
    to describe the table under it, not a different window, or the
    numbers won't add up when the operator checks them by hand.
    """

    count: int
    buys: int
    sells: int
    total_fees: Decimal
    net_usd: Decimal


@dataclass(frozen=True)
class StatusSnapshot:  # pylint: disable=too-many-instance-attributes
    """Everything the status template needs in one immutable bundle."""

    live_wired: bool
    open_orders: tuple[Order, ...]
    recent_trades: tuple[Trade, ...]
    last_fill_age_seconds: float | None
    # Latest market price per symbol that has open orders, sourced
    # from observe.db. Empty dict if observe.db isn't wired or no
    # snapshot landed in the last few minutes. Helps the operator
    # see "what those open orders are waiting on" at a glance.
    current_prices: dict[Symbol, Decimal] = field(default_factory=dict)
    # Trend direction over the same lookback window. "up" / "down"
    # render as a colored arrow; "flat" renders nothing. Symbols
    # missing from this dict (no snapshots in window) also render
    # nothing — graceful degrade, not an error.
    current_trends: dict[Symbol, TrendDirection] = field(default_factory=dict)
    # Per-order age in seconds since ``Order.created_at``. Keyed by
    # ``str(order.id)`` so templates can do ``snapshot.order_ages[o.id|string]``.
    # Helps the operator spot stale orders ("BUY has been sitting
    # 4d 7h while market is $1300 above it — should we re-anchor?").
    order_ages: dict[str, int] = field(default_factory=dict)
    # Per-symbol re-anchor recommendations from the drift + age
    # heuristic. Empty tuple = no banner. v1.0 ships info-only;
    # the action button (apply / snooze) is v1.1 and lands with
    # the operator-initiated re-anchor mechanism.
    reanchor_recommendations: tuple[ReanchorRecommendation, ...] = field(default_factory=tuple)
    # Sorted union of symbols seen in open_orders + recent_trades.
    # The template iterates this to render per-symbol sub-sections
    # with their own pause/resume icons (Stage 8.4.E soak Day 4).
    symbols: tuple[Symbol, ...] = field(default_factory=tuple)
    # Open orders grouped by symbol — saves the template from
    # filtering ``snapshot.open_orders`` N times per render.
    orders_by_symbol: dict[Symbol, tuple[Order, ...]] = field(default_factory=dict)
    # Completed BUY→SELL cycles reconstructed via FIFO matching
    # against ``recent_trades``. Newest-first; may be empty when no
    # cycles have completed yet. Template renders these in the
    # "Recent Cycles" panel below Recent Fills.
    recent_cycles: tuple[RecentCycle, ...] = field(default_factory=tuple)
    # Sum of cycle.net_pnl across cycles whose SELL fired today (UTC).
    # The "Today's PnL" scoreboard number — None when no realized PnL
    # has accrued today yet.
    today_realized_pnl: Decimal | None = None
    # All-time realized cycle PnL (sum of every matched cycle's net_pnl).
    # None when no cycles have completed.
    lifetime_realized_pnl: Decimal | None = None
    # ADR-040 follow-up: non-trade income (staking rewards), valued at
    # current prices. `None` = the ledger has not been ingested yet;
    # `unpriced_income_assets` names any asset earning income that the
    # dashboard has no price for, so it is visibly EXCLUDED rather than
    # silently valued at zero. BABY is the live example — staked but
    # never traded, so no price snapshot exists for it.
    staking_income_usd: Decimal | None = None
    staking_income_assets: tuple[str, ...] = ()
    unpriced_income_assets: tuple[str, ...] = ()
    # Aggregate account scoreboard (top-of-dashboard strip). Sourced from
    # observe.db's latest balance snapshot (credential-free per ADR-016)
    # + the observed prices above. All ``None`` when observe.db is
    # unwired/empty. ``held_value_usd`` = account_value − free USD (the
    # "in positions" exposure); ``balance_as_of`` carries the snapshot
    # time for a freshness stamp.
    free_usd: Decimal | None = None
    account_value_usd: Decimal | None = None
    held_value_usd: Decimal | None = None
    balance_as_of: datetime | None = None
    # Per-symbol price sparkline geometry (key = symbol). Symbols with
    # < 2 price snapshots in the 2h window are absent — the card renders
    # exactly as before (no misleading single-point line).
    sparklines: dict[Symbol, Sparkline] = field(default_factory=dict)
    # Session card (v1.1): the most recent session-loss-cap trip on
    # record (ADR-024's cap_trips table), so a trip that fires while
    # the operator misses the bell/Discord ping still has a durable
    # on-dashboard signal instead of disappearing once the alert
    # scrolls out of view. ``None`` when no trip has ever been
    # recorded. Age is precomputed here (not in the template) so
    # Jinja only formats, never does datetime arithmetic.
    last_cap_trip: CapTripRecord | None = None
    last_cap_trip_age_seconds: float | None = None
    # Whether the ADR-024 cool-down gate is active RIGHT NOW (blocks a
    # new cli/live session from starting) and, if so, when it lifts.
    # Both are ``False``/``None`` when no trip is on record, the
    # cool-down window is operator-disabled, or the window has elapsed.
    cool_down_active: bool = False
    cool_down_resumes_at: datetime | None = None
    # ADR-030: per-symbol engine visibility. Only FRESH rows (written
    # within ~3 live ticks) appear here — the loader drops stale rows
    # so a dead engine's last write can never render as current state.
    # Empty when operator.db is unwired, cli/live never wrote (its
    # operator_db unset), or every row aged out. The template asserts
    # paused/offside ONLY from entries present here; absence renders
    # nothing new.
    engine_states: dict[Symbol, EngineStateRow] = field(default_factory=dict)
    # Per-symbol held inventory (P3 slice 21) — the second half of the
    # buying-power item, whose aggregate strip shipped 2026-06-03. Same
    # observe.db balance snapshot the strip uses, split per symbol via
    # the shared ``held_by_symbol`` rule, so a card row can never
    # disagree with the "in positions" total above it. Empty when
    # observe.db is unwired.
    held_inventory: dict[Symbol, HeldInventory] = field(default_factory=dict)
    # Per-trade age in seconds, keyed by ``Trade.id`` — the fills
    # table's "age" column. Precomputed here so Jinja only formats and
    # never does datetime arithmetic (same rule as ``order_ages``).
    trade_ages: dict[str, int] = field(default_factory=dict)
    # Aggregate stats for exactly the fills in ``recent_trades``.
    # ``None`` when there are no fills to describe.
    fills_summary: FillsSummary | None = None
    error: str | None = None


def _empty_snapshot(*, wired: bool, error: str | None = None) -> StatusSnapshot:
    return StatusSnapshot(
        live_wired=wired,
        open_orders=(),
        recent_trades=(),
        last_fill_age_seconds=None,
        error=error,
    )


def _classify_trend(oldest: Decimal, newest: Decimal) -> TrendDirection:
    """Compare two prices; return up/down/flat based on the threshold."""
    if oldest <= 0:
        return "flat"
    delta_pct = (newest - oldest) / oldest
    if abs(delta_pct) <= _TREND_FLAT_THRESHOLD:
        return "flat"
    return "up" if delta_pct > 0 else "down"


async def _load_current_prices(
    observe_storage: StoragePort | None,
    symbols: set[Symbol],
) -> tuple[dict[Symbol, Decimal], dict[Symbol, TrendDirection]]:
    """Best-effort fetch of latest price + trend per symbol from observe.db.

    Returns ``({}, {})`` if observe.db is unwired. Per-symbol
    failures are logged + skipped rather than raised — a missing
    price is fine to display as a dash; raising would 500 the whole
    status card.

    Trend is computed by comparing the oldest and newest snapshots
    in the lookback window. Single-snapshot windows render "flat"
    (no signal yet).
    """
    if observe_storage is None or not symbols:
        return ({}, {})
    cutoff = datetime.now(UTC) - timedelta(minutes=_PRICE_LOOKBACK_MINUTES)
    prices: dict[Symbol, Decimal] = {}
    trends: dict[Symbol, TrendDirection] = {}
    for symbol in symbols:
        try:
            snapshots = await observe_storage.get_price_snapshots(symbol=symbol, start_time=cutoff)
        except StorageError as exc:
            _LOGGER.warning(
                "current-price lookup failed for %s; skipping",
                symbol,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        if not snapshots:
            continue
        sorted_snaps = sorted(snapshots, key=lambda s: s.observed_at.dt)
        oldest_price = sorted_snaps[0].price.amount
        latest_price = sorted_snaps[-1].price.amount
        prices[symbol] = latest_price
        trends[symbol] = (
            _classify_trend(oldest_price, latest_price) if len(sorted_snaps) >= 2 else "flat"
        )
    return prices, trends


def _build_sparkline(  # pylint: disable=too-many-locals
    series: list[Decimal],
    band_low: Decimal | None,
    band_high: Decimal | None,
    current: Decimal | None,
) -> Sparkline | None:
    """Project a price series (+ optional grid band + current) into SVG
    coords for the ``0 0 _SPARK_W _SPARK_H`` viewbox.

    Returns ``None`` when there aren't >= 2 points to draw a line from.
    The y-axis is inverted (higher price = lower y) and the domain spans
    every value that has to fit (series + band edges + current marker).
    """
    if len(series) < 2:
        return None
    values = list(series)
    for extra in (band_low, band_high, current):
        if extra is not None:
            values.append(extra)
    lo = min(values)
    hi = max(values)
    span = hi - lo
    inner_h = _SPARK_H - 2 * _SPARK_PAD
    inner_w = _SPARK_W - 2 * _SPARK_PAD

    def y_of(value: Decimal) -> float:
        if span == 0:
            return _SPARK_H / 2
        frac = float((value - lo) / span)  # 0 = lo (bottom), 1 = hi (top)
        return _SPARK_PAD + (1.0 - frac) * inner_h

    n = len(series)
    points = " ".join(
        f"{_SPARK_PAD + (i / (n - 1)) * inner_w:.1f},{y_of(v):.1f}" for i, v in enumerate(series)
    )

    band_y: float | None = None
    band_h: float | None = None
    if band_low is not None and band_high is not None and band_high >= band_low:
        band_y = y_of(band_high)
        band_h = max(y_of(band_low) - band_y, 0.5)

    marker_value = current if current is not None else series[-1]
    offside = (
        current is not None
        and band_low is not None
        and band_high is not None
        and (current < band_low or current > band_high)
    )
    return Sparkline(
        points=points,
        band_y=band_y,
        band_h=band_h,
        marker_x=_SPARK_W - _SPARK_PAD,
        marker_y=y_of(marker_value),
        offside=offside,
    )


async def _load_price_series(
    observe_storage: StoragePort | None,
    symbols: set[Symbol],
) -> dict[Symbol, list[Decimal]]:
    """Per-symbol price series (oldest->newest) over the sparkline window.

    Best-effort against observe.db (credential-free per ADR-016); a
    per-symbol storage failure logs + skips, and symbols with < 2 points
    are omitted so the sparkline degrades to nothing.
    """
    if observe_storage is None or not symbols:
        return {}
    cutoff = datetime.now(UTC) - timedelta(minutes=_SPARK_LOOKBACK_MINUTES)
    out: dict[Symbol, list[Decimal]] = {}
    for symbol in symbols:
        try:
            snaps = await observe_storage.get_price_snapshots(symbol=symbol, start_time=cutoff)
        except StorageError as exc:
            _LOGGER.warning(
                "sparkline series lookup failed; skipping (symbol=%s): %s",
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        if len(snaps) < 2:
            continue
        out[symbol] = [s.price.amount for s in sorted(snaps, key=lambda s: s.observed_at.dt)]
    return out


def _build_sparklines(
    series_by_symbol: dict[Symbol, list[Decimal]],
    orders_by_symbol: dict[Symbol, tuple[Order, ...]],
    prices: dict[Symbol, Decimal],
) -> dict[Symbol, Sparkline]:
    """Build per-symbol sparkline geometry. Band = open-order ladder
    (lowest -> highest order price); absent for parked / no-order symbols."""
    out: dict[Symbol, Sparkline] = {}
    for symbol, series in series_by_symbol.items():
        order_prices = [o.price.amount for o in orders_by_symbol.get(symbol, ())]
        spark = _build_sparkline(
            series,
            min(order_prices) if order_prices else None,
            max(order_prices) if order_prices else None,
            prices.get(symbol),
        )
        if spark is not None:
            out[symbol] = spark
    return out


async def _load_balances(observe_storage: StoragePort | None) -> list[Balance]:
    """Latest balance snapshot from observe.db; ``[]`` when unwired/failed.

    Per ADR-016 the web tier stays credential-free — balances come from
    the observe.db snapshot cli/observe polls, not a live Kraken call. A
    storage failure degrades to an empty list (the scoreboard renders
    "—") rather than 500ing the dashboard.
    """
    if observe_storage is None:
        return []
    try:
        return await observe_storage.get_latest_balance_snapshot()
    except StorageError as exc:
        _LOGGER.warning(
            "balance snapshot lookup failed; skipping: %s",
            exc,
            extra={"error": str(exc)},
        )
        return []


def held_by_symbol(
    balances: list[Balance],
    prices: dict[Symbol, Decimal],
) -> dict[Symbol, HeldInventory]:
    """Per-symbol held inventory, valued at the observed price.

    The single definition of "what counts as held and what it's
    worth" — the scoreboard's aggregate ``in positions`` figure is
    derived from THIS map (see ``_compute_balance_metrics``), so a
    per-symbol row can never disagree with the total above it.

    USD is excluded (it's the quote side, reported as free/total, not
    inventory) and so are zero balances. An asset with no observed
    price still gets an entry with ``value_usd=None``: the operator
    holds it, and saying "0.0013 BTC, price unknown" beats pretending
    the position isn't there.
    """
    held: dict[Symbol, HeldInventory] = {}
    for bal in balances:
        if bal.asset == "USD" or bal.total <= 0:
            continue
        symbol = Symbol(base=bal.asset, quote="USD")
        price = prices.get(symbol)
        held[symbol] = HeldInventory(
            amount=bal.total,
            value_usd=bal.total * price if price is not None else None,
        )
    return held


def _summarize_fills(trades: Sequence[Trade]) -> FillsSummary | None:
    """Aggregate the fills the table is about to render.

    ``net_usd`` is SIGNED: a SELL adds ``cost − fee`` (USD in), a BUY
    subtracts ``cost + fee`` (USD out). The per-row cell renders the
    same two quantities but carries direction in an arrow rather than
    a sign, so summing that column verbatim would total gross churn
    and label it "net" — a number that reads like profit and isn't.
    Positive here means USD flowed into the account across these
    fills. Returns ``None`` for an empty window — nothing to describe.
    """
    if not trades:
        return None
    buys = sum(1 for t in trades if t.side.value == "buy")
    net = Decimal(0)
    fees = Decimal(0)
    for trade in trades:
        fees += trade.fee
        net += -(trade.cost + trade.fee) if trade.side.value == "buy" else trade.cost - trade.fee
    return FillsSummary(
        count=len(trades),
        buys=buys,
        sells=len(trades) - buys,
        total_fees=fees,
        net_usd=net,
    )


def _compute_balance_metrics(
    balances: list[Balance],
    prices: dict[Symbol, Decimal],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Derive (free_usd, account_value, held_value) from a balance snapshot.

    ``account_value`` = USD total + Σ(held base × observed price);
    ``free_usd`` = the USD balance's available; ``held_value`` =
    account − USD total (the "in positions" exposure). Held assets
    without a known price are omitted from the valuation — a slight
    undercount beats blocking the whole card on one missing price.
    Returns all-``None`` when no balance snapshot is available.
    """
    if not balances:
        return (None, None, None)
    usd_total = Decimal(0)
    free_usd = Decimal(0)
    for bal in balances:
        if bal.asset == "USD":
            usd_total = bal.total
            free_usd = bal.available
            break
    held_value = sum(
        (h.value_usd for h in held_by_symbol(balances, prices).values() if h.value_usd is not None),
        Decimal(0),
    )
    return (free_usd, usd_total + held_value, held_value)


async def _load_staking_income(
    operator_storage: StoragePort | None,
    prices: dict[Symbol, Decimal],
) -> tuple[Decimal | None, tuple[str, ...], tuple[str, ...]]:
    """Non-trade income (ADR-040 follow-up), valued at current prices.

    Returns ``(total_usd, income_assets, unpriced_assets)``; a ``None``
    total means "nothing ingested yet", which the template renders as
    absent rather than as zero.

    Lives in operator.db (ADR-014 decision 5's shared-ledger precedent),
    so an unwired or unreadable store degrades to "not shown" — the same
    posture every other cross-DB card here takes.
    """
    if operator_storage is None:
        return None, (), ()
    try:
        ledger = await operator_storage.get_ledger_entries(limit=_LEDGER_FETCH_LIMIT)
    except StorageError as exc:
        _LOGGER.warning(
            "ledger read failed; staking income omitted from status: %s",
            exc,
            extra={"error": str(exc)},
        )
        return None, (), ()
    if not ledger:
        return None, (), ()
    by_asset = income_by_asset(ledger)
    # Price map keyed by ASSET, from the symbols the dashboard already
    # priced. An income asset absent here is reported as unpriced, never
    # valued at zero — BABY is the live case: staked but never traded,
    # so no price snapshot exists for it.
    asset_prices = {sym.base: price for sym, price in prices.items()}
    total, unpriced = value_income_usd(by_asset, asset_prices)
    return total, tuple(sorted(by_asset)), unpriced


async def _load_snapshot(  # pylint: disable=too-many-locals,too-many-arguments
    live_storage: StoragePort | None,
    observe_storage: StoragePort | None,
    *,
    operator_tz: str = "UTC",
    cool_down_minutes: float | None = None,
    operator_storage: StoragePort | None = None,
    live_tick_seconds: float | None = None,
    reanchor_min_severity: ReanchorSeverity = "mild",
) -> StatusSnapshot:
    """Pull open orders + recent fills + current prices; degrade gracefully.

    ``operator_tz`` scopes the "today" filter for the realized-PnL
    header so the day boundary matches the operator-tz timestamps
    rendered on cycle rows. Without this, the header rolled over at
    UTC midnight while the operator's CST clock still read late
    evening on the same calendar day, silently showing "Today: $0.00"
    against cycles that the operator considered today's.

    ``cool_down_minutes`` mirrors ``LiveConfig.cool_down_minutes``
    (ADR-024) so the session card can show whether a new session is
    currently gated — ``None`` when the operator disabled the gate
    or ``cli/web`` wasn't given a ``live:`` config section to read it
    from (the trip itself still shows; only the "gate active" framing
    is unavailable).
    """
    if live_storage is None:
        return _empty_snapshot(wired=False)
    try:
        open_orders = await live_storage.get_open_orders()
        # Wide window: match cycles over the full trade history for the
        # lifetime-PnL aggregate + correct FIFO pairing; slice the newest
        # few for Recent Fills. One query, several views.
        all_recent = await live_storage.get_trades(limit=_TRADE_FETCH_LIMIT)
    except StorageError as exc:
        return _empty_snapshot(wired=True, error=f"failed to query live.db: {exc}")
    recent = all_recent[:_RECENT_FILLS_DISPLAY]
    cycles = tuple(match_cycles(all_recent))
    today_pnl = today_realized_pnl(cycles, tz_name=operator_tz) if cycles else None
    lifetime_pnl = sum((c.net_pnl for c in cycles), Decimal(0)) if cycles else None
    last_age: float | None = None
    if recent:
        most_recent = max(recent, key=lambda t: t.executed_at.dt)
        delta = datetime.now(UTC) - most_recent.executed_at.dt
        last_age = delta.total_seconds()
    symbols_with_orders = {o.symbol for o in open_orders}
    # Latest balance snapshot from observe.db (credential-free per ADR-016).
    # Held bases join the price-fetch set so the account-value math can
    # value inventory that isn't currently trading (e.g. parked BTC).
    balances = await _load_balances(observe_storage)
    held_symbols = {
        Symbol(base=b.asset, quote="USD") for b in balances if b.asset != "USD" and b.total > 0
    }
    # Fetch prices for EVERY symbol that will render a card (orders ∪
    # recent trades) plus held bases — so a parked / no-order symbol
    # (e.g. BTC offside) still shows its price + trend, not a bare name.
    trade_symbols = {t.symbol for t in recent}
    prices, trends = await _load_current_prices(
        observe_storage, symbols_with_orders | held_symbols | trade_symbols
    )
    free_usd, account_value, held_value = _compute_balance_metrics(balances, prices)
    held_inventory = held_by_symbol(balances, prices)
    balance_as_of = balances[0].updated_at.dt if balances else None
    now = datetime.now(UTC)
    order_ages = {str(o.id): int((now - o.created_at.dt).total_seconds()) for o in open_orders}
    trade_ages = {t.id: int((now - t.executed_at.dt).total_seconds()) for t in recent}
    fills_summary = _summarize_fills(recent)
    # A card renders for every symbol you have orders for, hold a balance
    # in, or traded recently — so a parked/held coin (e.g. offside BTC)
    # keeps its card + price even when its last fill ages out of the
    # display window. Sorted by (base, quote) for stable order.
    all_symbols = tuple(
        sorted(
            symbols_with_orders | held_symbols | {t.symbol for t in recent},
            key=lambda s: (s.base, s.quote),
        )
    )
    # One series fetch serves both the sparklines and the banner's
    # activity stat (recent range in spacings).
    price_series = await _load_price_series(observe_storage, set(all_symbols))
    snoozed = await load_reanchor_snoozes(operator_storage, now=now)
    reanchor_recs = await load_reanchor_recommendations(
        live_storage,
        list(open_orders),
        prices,
        order_ages,
        snoozed,
        price_series,
        observe_storage,
        operator_storage,
        reanchor_min_severity,
    )
    # ADR-040 follow-up: income the trades table cannot see. Lives in
    # operator.db (ADR-014 decision 5's shared-ledger precedent), so it
    # degrades to "not shown" rather than erroring when that DB is
    # unwired -- same posture as every other cross-DB card here.
    income_usd, income_assets, unpriced_assets = await _load_staking_income(
        operator_storage, prices
    )

    last_cap_trip = await _load_last_cap_trip(live_storage)
    engine_states = await _load_engine_states(
        operator_storage, tick_seconds=live_tick_seconds, now=now
    )
    last_cap_trip_age: float | None = None
    cool_down = check_cool_down(
        last_cap_trip.tripped_at if last_cap_trip else None,
        now=now,
        window_minutes=cool_down_minutes,
    )
    if last_cap_trip is not None:
        last_cap_trip_age = (now - last_cap_trip.tripped_at.dt).total_seconds()
    orders_by_symbol: dict[Symbol, tuple[Order, ...]] = {
        sym: tuple(o for o in open_orders if o.symbol == sym) for sym in all_symbols
    }
    sparklines = _build_sparklines(price_series, orders_by_symbol, prices)
    return StatusSnapshot(
        live_wired=True,
        open_orders=tuple(open_orders),
        recent_trades=tuple(recent),
        last_fill_age_seconds=last_age,
        current_prices=prices,
        current_trends=trends,
        order_ages=order_ages,
        reanchor_recommendations=reanchor_recs,
        symbols=all_symbols,
        orders_by_symbol=orders_by_symbol,
        recent_cycles=cycles[:_RECENT_CYCLES_DISPLAY],
        today_realized_pnl=today_pnl,
        lifetime_realized_pnl=lifetime_pnl,
        staking_income_usd=income_usd,
        staking_income_assets=income_assets,
        unpriced_income_assets=unpriced_assets,
        free_usd=free_usd,
        account_value_usd=account_value,
        held_value_usd=held_value,
        balance_as_of=balance_as_of,
        sparklines=sparklines,
        last_cap_trip=last_cap_trip,
        last_cap_trip_age_seconds=last_cap_trip_age,
        cool_down_active=cool_down.active,
        cool_down_resumes_at=cool_down.resumes_at,
        engine_states=engine_states,
        held_inventory=held_inventory,
        trade_ages=trade_ages,
        fills_summary=fills_summary,
    )


async def _load_last_cap_trip(live_storage: StoragePort) -> CapTripRecord | None:
    """The most recent session-loss-cap trip (ADR-024), or ``None``.

    A storage failure here degrades to "no trip on record" rather than
    failing the whole snapshot — the session card is a durable-signal
    nicety, not load-bearing for the rest of the dashboard.
    """
    try:
        return await live_storage.get_last_cap_trip()
    except StorageError as exc:
        _LOGGER.warning(
            "last-cap-trip lookup failed; session card omitted: %s",
            exc,
            extra={"error": str(exc)},
        )
        return None


async def _load_engine_states(
    operator_storage: StoragePort | None,
    *,
    tick_seconds: float | None,
    now: datetime,
) -> dict[Symbol, EngineStateRow]:
    """FRESH engine_state rows keyed by symbol (ADR-030); stale dropped.

    Freshness = ``updated_at`` within ``_ENGINE_STATE_FRESHNESS_TICKS``
    of the writer's tick cadence. Rows from a dead or stopped engine
    age out here — the badge layer asserts nothing it can't currently
    know. A storage failure degrades to "no state" (same posture as
    :func:`_load_last_cap_trip`); "no rows at all" and "operator.db
    unwired" land on the identical safe default.
    """
    if operator_storage is None:
        return {}
    try:
        rows = await operator_storage.get_engine_states()
    except StorageError as exc:
        _LOGGER.warning(
            "engine-state lookup failed; state badges omitted: %s",
            exc,
            extra={"error": str(exc)},
        )
        return {}
    tick = tick_seconds if tick_seconds is not None else _DEFAULT_TICK_SECONDS
    threshold_seconds = _ENGINE_STATE_FRESHNESS_TICKS * tick
    return {
        row.symbol: row
        for row in rows
        if (now - row.updated_at).total_seconds() <= threshold_seconds
    }


# --------------------------------------------------------------------- #
# Dashboard root replaces the 7.1 stub                                  #
# --------------------------------------------------------------------- #


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    user: User = Depends(require_user),
    live_storage: StoragePort | None = Depends(get_live_storage),
    observe_storage: StoragePort | None = Depends(get_observe_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
    cool_down_minutes: float | None = Depends(get_cool_down_minutes),
    operator_storage: StoragePort = Depends(get_operator_storage),
    live_tick_seconds: float | None = Depends(get_live_tick_seconds),
    reanchor_min_severity: ReanchorSeverity = Depends(get_reanchor_min_severity),
) -> Response:
    """Combined dashboard — cost card + open orders + recent fills.

    Health snapshot no longer loaded here — the status-card traffic
    light was removed 2026-05-23 and the navbar heart-pulse icon's
    tiered dot now polls /health/overall.json directly.
    """
    snapshot = await _load_snapshot(
        live_storage,
        observe_storage,
        operator_tz=prefs.timezone,
        cool_down_minutes=cool_down_minutes,
        operator_storage=operator_storage,
        live_tick_seconds=live_tick_seconds,
        reanchor_min_severity=reanchor_min_severity,
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
        },
    )


@router.get("/status/card", response_class=HTMLResponse)
async def status_card(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    live_storage: StoragePort | None = Depends(get_live_storage),
    observe_storage: StoragePort | None = Depends(get_observe_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
    cool_down_minutes: float | None = Depends(get_cool_down_minutes),
    operator_storage: StoragePort = Depends(get_operator_storage),
    live_tick_seconds: float | None = Depends(get_live_tick_seconds),
    reanchor_min_severity: ReanchorSeverity = Depends(get_reanchor_min_severity),
) -> Response:
    """HTMX fragment — open-orders + recent-fills card without chrome.

    No health snapshot — the navbar dot owns health UX since
    2026-05-23 (single source of truth via /health/overall.json).
    """
    snapshot = await _load_snapshot(
        live_storage,
        observe_storage,
        operator_tz=prefs.timezone,
        cool_down_minutes=cool_down_minutes,
        operator_storage=operator_storage,
        live_tick_seconds=live_tick_seconds,
        reanchor_min_severity=reanchor_min_severity,
    )
    return templates.TemplateResponse(
        request,
        "_status_card.html",
        {
            "snapshot": snapshot,
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
        },
    )


@router.get("/status/recent-fills.json")
async def recent_fills_json(
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    live_storage: StoragePort | None = Depends(get_live_storage),
) -> Response:
    """Compact newest-first JSON of recent fills, for the client toast popper.

    Auth-gated like every status surface. ``live.db`` unwired or a query
    failure returns an empty list rather than erroring — the toast poll is
    best-effort UX, never load-bearing.
    """
    if live_storage is None:
        return JSONResponse({"fills": []})
    try:
        trades = await live_storage.get_trades(limit=20)
    except StorageError:
        return JSONResponse({"fills": []})
    return JSONResponse(
        {
            "fills": [
                {
                    "id": t.id,
                    "symbol": f"{t.symbol.base}/{t.symbol.quote}",
                    "side": t.side.value,
                    # Pre-rendered display strings (the toast shows them
                    # verbatim): house formatters, so a DOGE fill reads
                    # "$0.0698", not the "$0.07" that fixed 2dp made of it.
                    "price": fmt_usd(t.price.amount),
                    "amount": fmt_qty(t.amount.value),
                }
                for t in trades
            ]
        }
    )


__all__ = ("router", "StatusSnapshot")

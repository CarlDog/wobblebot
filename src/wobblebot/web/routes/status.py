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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cycle_matcher import RecentCycle, match_cycles, today_realized_pnl
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_live_storage,
    get_observe_storage,
    get_templates,
)

_LOGGER = logging.getLogger(__name__)
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
# Cap on cycles rendered in the Recent Cycles table (the full set still
# feeds the lifetime-PnL aggregate). Keeps the table bounded once the
# match window covers the whole history.
_RECENT_CYCLES_DISPLAY = 10

TrendDirection = Literal["up", "down", "flat"]
ReanchorSeverity = Literal["mild", "moderate", "strong"]

# Re-anchor banner thresholds. Drift is the gate (no drift = no
# banner, even if grid is stale); age can escalate severity but
# can't trigger alone. Calm-market parked grids don't get scary
# banners; misaligned grids do.
_DRIFT_MILD_SPACINGS = 1.5
_DRIFT_MODERATE_SPACINGS = 2.5
_DRIFT_STRONG_SPACINGS = 4.0
_AGE_MILD_HOURS = 24
_AGE_MODERATE_HOURS = 48
_AGE_STRONG_HOURS = 72

router = APIRouter(tags=["status"])


@dataclass(frozen=True)
class ReanchorRecommendation:
    """Per-symbol recommendation that the operator consider re-anchoring.

    Rendered as a colored banner on the dashboard. Severity tier
    drives the color: mild (yellow) -> moderate (orange) ->
    strong (red). Action button + snooze are v1.1 candidates;
    v1.0 ships info-only.
    """

    symbol: Symbol
    severity: ReanchorSeverity
    drift_in_spacings: float
    oldest_order_age_seconds: int
    current_price: Decimal
    anchor_price: Decimal


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


def _classify_reanchor_severity(drift_spacings: float, age_seconds: int) -> ReanchorSeverity | None:
    """Return severity tier, or ``None`` if no banner should show.

    Drift is the gate: without meaningful drift the engine isn't
    misaligned, so a banner would be noise even if the grid has
    been sitting for days. Age escalates severity but doesn't
    trigger alone — a calm market with a parked grid is normal.
    """
    if drift_spacings < _DRIFT_MILD_SPACINGS:
        return None
    if drift_spacings < _DRIFT_MODERATE_SPACINGS:
        drift_tier = 1
    elif drift_spacings < _DRIFT_STRONG_SPACINGS:
        drift_tier = 2
    else:
        drift_tier = 3
    age_hours = age_seconds / 3600
    if age_hours < _AGE_MILD_HOURS:
        age_tier = 0
    elif age_hours < _AGE_MODERATE_HOURS:
        age_tier = 1
    elif age_hours < _AGE_STRONG_HOURS:
        age_tier = 2
    else:
        age_tier = 3
    tier = max(drift_tier, age_tier)
    if tier == 1:
        return "mild"
    if tier == 2:
        return "moderate"
    return "strong"


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
        _LOGGER.warning("balance snapshot lookup failed; skipping", extra={"error": str(exc)})
        return []


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
    held_value = Decimal(0)
    for bal in balances:
        if bal.asset == "USD":
            usd_total = bal.total
            free_usd = bal.available
            continue
        if bal.total <= 0:
            continue
        price = prices.get(Symbol(base=bal.asset, quote="USD"))
        if price is not None:
            held_value += bal.total * price
    return (free_usd, usd_total + held_value, held_value)


async def _load_snapshot(  # pylint: disable=too-many-locals
    live_storage: StoragePort | None,
    observe_storage: StoragePort | None,
    *,
    operator_tz: str = "UTC",
) -> StatusSnapshot:
    """Pull open orders + recent fills + current prices; degrade gracefully.

    ``operator_tz`` scopes the "today" filter for the realized-PnL
    header so the day boundary matches the operator-tz timestamps
    rendered on cycle rows. Without this, the header rolled over at
    UTC midnight while the operator's CST clock still read late
    evening on the same calendar day, silently showing "Today: $0.00"
    against cycles that the operator considered today's.
    """
    if live_storage is None:
        return _empty_snapshot(wired=False)
    try:
        open_orders = await live_storage.get_open_orders()
        # Wide window: match cycles over the full trade history for the
        # lifetime-PnL aggregate + correct FIFO pairing; slice the first
        # 20 for Recent Fills. One query, several views.
        all_recent = await live_storage.get_trades(limit=_TRADE_FETCH_LIMIT)
    except StorageError as exc:
        return _empty_snapshot(wired=True, error=f"failed to query live.db: {exc}")
    recent = all_recent[:20]
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
    balance_as_of = balances[0].updated_at.dt if balances else None
    now = datetime.now(UTC)
    order_ages = {str(o.id): int((now - o.created_at.dt).total_seconds()) for o in open_orders}
    reanchor_recs = await _load_reanchor_recommendations(
        live_storage, list(open_orders), prices, order_ages
    )
    # Union of symbols seen in open orders + recent trades, sorted
    # by (base, quote) for stable rendering order.
    all_symbols = tuple(
        sorted(
            symbols_with_orders | {t.symbol for t in recent},
            key=lambda s: (s.base, s.quote),
        )
    )
    orders_by_symbol: dict[Symbol, tuple[Order, ...]] = {
        sym: tuple(o for o in open_orders if o.symbol == sym) for sym in all_symbols
    }
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
        free_usd=free_usd,
        account_value_usd=account_value,
        held_value_usd=held_value,
        balance_as_of=balance_as_of,
    )


async def _load_reanchor_recommendations(  # pylint: disable=too-many-locals
    live_storage: StoragePort,
    open_orders: list[Order],
    current_prices: dict[Symbol, Decimal],
    order_ages: dict[str, int],
) -> tuple[ReanchorRecommendation, ...]:
    """Per-symbol re-anchor recommendations from drift + age heuristic.

    Drift = distance from current price to the nearest open order,
    expressed in units of grid spacing. Age = oldest open order
    for that symbol. ``_classify_reanchor_severity`` gates and
    tiers; per-symbol storage failures are logged + skipped.
    """
    if not open_orders:
        return ()
    symbols = {o.symbol for o in open_orders}
    recommendations: list[ReanchorRecommendation] = []
    for symbol in symbols:
        current = current_prices.get(symbol)
        if current is None:
            continue
        try:
            state = await live_storage.get_grid_state(symbol)
        except StorageError as exc:
            _LOGGER.warning(
                "grid_state lookup failed for %s; skipping reanchor calc",
                symbol,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        if state is None:
            continue
        spacing = state.reference_price * state.spacing_percentage / Decimal("100")
        if spacing <= 0:
            continue
        symbol_orders = [o for o in open_orders if o.symbol == symbol]
        if not symbol_orders:
            continue
        nearest_distance = min(abs(o.price.amount - current) for o in symbol_orders)
        drift = float(nearest_distance / spacing)
        oldest_age = max(order_ages.get(str(o.id), 0) for o in symbol_orders)
        severity = _classify_reanchor_severity(drift, oldest_age)
        if severity is None:
            continue
        recommendations.append(
            ReanchorRecommendation(
                symbol=symbol,
                severity=severity,
                drift_in_spacings=drift,
                oldest_order_age_seconds=oldest_age,
                current_price=current,
                anchor_price=state.reference_price,
            )
        )
    return tuple(recommendations)


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
) -> Response:
    """Combined dashboard — cost card + open orders + recent fills.

    Health snapshot no longer loaded here — the status-card traffic
    light was removed 2026-05-23 and the navbar heart-pulse icon's
    tiered dot now polls /health/overall.json directly.
    """
    snapshot = await _load_snapshot(live_storage, observe_storage, operator_tz=prefs.timezone)
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
) -> Response:
    """HTMX fragment — open-orders + recent-fills card without chrome.

    No health snapshot — the navbar dot owns health UX since
    2026-05-23 (single source of truth via /health/overall.json).
    """
    snapshot = await _load_snapshot(live_storage, observe_storage, operator_tz=prefs.timezone)
    return templates.TemplateResponse(
        request,
        "_status_card.html",
        {
            "snapshot": snapshot,
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
        },
    )


__all__ = ("router", "StatusSnapshot")

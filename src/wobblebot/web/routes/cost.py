"""Cost dashboard — reads operator.db's llm_calls + live.db's trades
(Stage 7.2.A + Stage 8.4 trading-fees follow-up).

Two cost surfaces:

1. **LLM cost** (Stage 7.2.A; from operator.db's llm_calls). Per
   ADR-014 every cloud-LLM call writes a forensic
   ``LLMCallRecord`` row. Surfaced as: 24h total + 7d total +
   per-day rollup + per-provider/role rollup.
2. **Kraken trading fees** (Stage 8.4 follow-up; from live.db's
   trades). Each completed fill stores its maker/taker fee in the
   `fee` column. Surfaced as: rolling sums for 24h / 7d / 30d /
   all-time so the operator can see real per-period trading
   costs without doing the math against the trades table by hand.

Routes:

- ``GET /cost`` — full page rendered against ``cost.html``.
- ``GET /cost/card`` — fragment for HTMX-polled refresh from the
  dashboard. Same data, no layout chrome.

Both are auth-gated; no mutations. Both gracefully degrade when
their respective storage adapter is ``None`` — the LLM cost card
shows an empty rollup; the trading-fees card shows an "unwired"
placeholder.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.config.cli import WebConfig
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.models import Trade
from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cycle_matcher import RecentCycle, match_cycles
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_config,
    get_live_storage,
    get_operator_storage,
    get_templates,
)

router = APIRouter(tags=["cost"])

# The by-evaluation table caps traced rows (untraced renders as one
# aggregate row on top of the cap) — at ~144 evaluations/day a full
# listing would drown the card.
_TRACE_ROWS_CAP = 20

# Annualization factor for the cost-honesty projection: the 30d
# window's net, scaled to a year. A projection, not a promise — the
# card's copy says which window it extrapolates.
_DAYS_PER_YEAR = Decimal("365")
_HONESTY_WINDOWS = ((7, "last 7d"), (30, "last 30d"))


# --------------------------------------------------------------------- #
# Rollups                                                               #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class DayRollup:
    """Total cost + call count for one calendar day (UTC)."""

    day: str  # ISO YYYY-MM-DD
    cost_usd: Decimal
    call_count: int


@dataclass(frozen=True)
class GroupRollup:
    """Total cost + call count for one provider/role/model triple."""

    key: str  # display label like "anthropic / quant"
    cost_usd: Decimal
    call_count: int


@dataclass(frozen=True)
class TraceRollup:
    """Total cost + call count for one advisory evaluation (P4.7).

    Groups the 24h ``llm_calls`` slice by ``trace_id`` (P4.4a stamps
    one per advise symbol-evaluation). The ``untraced`` bucket holds
    NULL-trace rows — everything written before the trace clock
    started, plus non-advise callers (cli/operator today).
    """

    trace_label: str  # short trace id, or "untraced"
    cost_usd: Decimal
    call_count: int
    roles: str  # distinct roles in the group, comma-joined


@dataclass(frozen=True)
class CostSnapshot:  # pylint: disable=too-many-instance-attributes
    """Everything the cost template needs in one immutable bundle.

    ``cached_tokens_24h`` is the 24h sum of ``tokens_cache_read``
    (ADR-033) — prompt tokens served from provider caches at a
    discounted rate. Nonzero means automatic caching is saving money;
    the per-token discount is already reflected in the cost totals.
    """

    total_24h_usd: Decimal
    total_7d_usd: Decimal
    call_count_24h: int
    call_count_7d: int
    cached_tokens_24h: int
    per_day: tuple[DayRollup, ...]
    per_provider_role: tuple[GroupRollup, ...]
    per_trace: tuple[TraceRollup, ...] = ()
    error: str | None = None


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class TradingFeesSnapshot:
    """Kraken trading fees rolled up across the standard windows.

    Fee currency is the trade's quote currency. All current configured
    pairs (BTC/USD, DOGE/USD, etc.) settle fees in USD, so summing
    ``trade.fee`` across the window yields USD totals. If a non-USD
    quote pair ever ships, this rollup will need per-currency handling.

    Trade counts are reported alongside dollars so the operator can
    see "did I pay $5 across 100 cycles or across 5 cycles" (high
    cycle count + low fee per = healthy maker-side execution; low
    cycle count + high fee per = chasing the price with taker
    fills).
    """

    live_wired: bool
    total_24h_usd: Decimal
    total_7d_usd: Decimal
    total_30d_usd: Decimal
    total_all_time_usd: Decimal
    trade_count_24h: int
    trade_count_7d: int
    trade_count_30d: int
    trade_count_all_time: int
    error: str | None = None


@dataclass(frozen=True)
class HonestyWindow:
    """One window's honest bottom line (P4.7).

    ``realized_pnl_usd`` comes from matched cycles and is ALREADY net
    of both legs' trading fees (``cycle_matcher`` subtracts them per
    cycle) — the ledger must not subtract fees again, so the card
    lists trading fees as an informational line, never as a deduction.
    ``infra_cost_usd`` is ``None`` when the operator hasn't declared
    ``web.cost_assumptions.monthly_infra_usd``; ``net_usd`` then
    excludes infra and the card says so.
    """

    label: str
    days: int
    realized_pnl_usd: Decimal
    cycle_count: int
    llm_cost_usd: Decimal
    infra_cost_usd: Decimal | None
    net_usd: Decimal


@dataclass(frozen=True)
class CostHonestySnapshot:
    """Realized PnL beside every cost the operator actually pays."""

    live_wired: bool
    infra_declared: bool
    windows: tuple[HonestyWindow, ...]
    annualized_net_usd: Decimal | None  # 30d net × 365/30; None when unwired
    error: str | None = None


def _empty_honesty_snapshot(*, wired: bool, error: str | None = None) -> CostHonestySnapshot:
    return CostHonestySnapshot(
        live_wired=wired,
        infra_declared=False,
        windows=(),
        annualized_net_usd=None,
        error=error,
    )


def _rollup_honesty(
    cycles: list[RecentCycle],
    llm_rows: list[LLMCallRecord],
    *,
    now: datetime,
    monthly_infra_usd: Decimal | None,
) -> CostHonestySnapshot:
    """The honest bottom line per window: PnL − LLM − infra.

    Cycle PnL buckets by SELL time (PnL is realized at the sell, the
    ``today_realized_pnl`` convention); LLM cost by call timestamp;
    infra pro-rates the declared monthly figure at 30 days/month.
    """
    windows: list[HonestyWindow] = []
    net_30d: Decimal | None = None
    for days, label in _HONESTY_WINDOWS:
        cutoff = now - timedelta(days=days)
        window_cycles = [c for c in cycles if c.sell_executed_at.dt >= cutoff]
        pnl = sum((c.net_pnl for c in window_cycles), Decimal("0"))
        llm = sum(
            (r.cost_usd for r in llm_rows if r.timestamp.dt >= cutoff),
            Decimal("0"),
        )
        infra: Decimal | None = None
        if monthly_infra_usd is not None:
            infra = monthly_infra_usd / Decimal("30") * Decimal(days)
        net = pnl - llm - (infra or Decimal("0"))
        windows.append(
            HonestyWindow(
                label=label,
                days=days,
                realized_pnl_usd=pnl,
                cycle_count=len(window_cycles),
                llm_cost_usd=llm,
                infra_cost_usd=infra,
                net_usd=net,
            )
        )
        if days == 30:
            net_30d = net
    annualized = net_30d * _DAYS_PER_YEAR / Decimal("30") if net_30d is not None else None
    return CostHonestySnapshot(
        live_wired=True,
        infra_declared=monthly_infra_usd is not None,
        windows=tuple(windows),
        annualized_net_usd=annualized,
    )


def _empty_snapshot(error: str | None = None) -> CostSnapshot:
    return CostSnapshot(
        total_24h_usd=Decimal("0"),
        total_7d_usd=Decimal("0"),
        call_count_24h=0,
        call_count_7d=0,
        cached_tokens_24h=0,
        per_day=(),
        per_provider_role=(),
        error=error,
    )


def _empty_fees_snapshot(*, wired: bool, error: str | None = None) -> TradingFeesSnapshot:
    return TradingFeesSnapshot(
        live_wired=wired,
        total_24h_usd=Decimal("0"),
        total_7d_usd=Decimal("0"),
        total_30d_usd=Decimal("0"),
        total_all_time_usd=Decimal("0"),
        trade_count_24h=0,
        trade_count_7d=0,
        trade_count_30d=0,
        trade_count_all_time=0,
        error=error,
    )


def _rollup_fees(  # pylint: disable=too-many-locals
    trades: list[Trade], *, now: datetime
) -> TradingFeesSnapshot:
    """Bucket fees + trade counts into the four standard windows.

    Single-pass sum over the input list. Trades are bucketed by
    `executed_at`, not by storage `created_at` — the fee was paid
    when Kraken matched the order, not when the row was persisted.
    """
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    total_24h = Decimal("0")
    total_7d = Decimal("0")
    total_30d = Decimal("0")
    total_all = Decimal("0")
    n_24h = 0
    n_7d = 0
    n_30d = 0
    n_all = 0

    for trade in trades:
        ts = trade.executed_at.dt
        fee = trade.fee
        total_all += fee
        n_all += 1
        if ts >= cutoff_30d:
            total_30d += fee
            n_30d += 1
            if ts >= cutoff_7d:
                total_7d += fee
                n_7d += 1
                if ts >= cutoff_24h:
                    total_24h += fee
                    n_24h += 1

    return TradingFeesSnapshot(
        live_wired=True,
        total_24h_usd=total_24h,
        total_7d_usd=total_7d,
        total_30d_usd=total_30d,
        total_all_time_usd=total_all,
        trade_count_24h=n_24h,
        trade_count_7d=n_7d,
        trade_count_30d=n_30d,
        trade_count_all_time=n_all,
    )


async def _load_trading_fees_snapshot(
    live_storage: StoragePort | None,
) -> TradingFeesSnapshot:
    """Pull every trade from live.db + roll up by window.

    When live_storage is None, the live_db cross-DB path wasn't
    configured — return an "unwired" snapshot so the template can
    render a placeholder. Storage failures degrade to an error
    snapshot rather than 500-ing the whole cost page.
    """
    if live_storage is None:
        return _empty_fees_snapshot(wired=False)
    try:
        # High limit covers years of soak-rate trading. If the
        # operator ever does enough trades to bump this, the right
        # fix is paginated streaming sums, not a higher limit —
        # but for v1.0 grid-strategy traffic 10k is comfortable.
        trades = await live_storage.get_trades(limit=10_000)
    except StorageError as exc:
        return _empty_fees_snapshot(wired=True, error=f"failed to query trades: {exc}")
    return _rollup_fees(trades, now=datetime.now(UTC))


# pylint: disable=too-many-locals
def _rollup(rows: list[LLMCallRecord], *, now: datetime) -> CostSnapshot:
    """Compute the snapshot from the last 7 days of llm_calls rows.

    Two windows: a 24h slice (for the prominent total) and the full
    7-day slice (for the per-day bars). Per-provider/role grouping
    runs on the 24h slice — short-window patterns are what an operator
    monitoring spend actually wants to see.
    """
    cutoff_24h = now - timedelta(hours=24)

    total_24h = Decimal("0")
    total_7d = Decimal("0")
    n_24h = 0
    n_7d = 0
    cached_24h = 0
    by_day: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))
    by_group: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))
    by_trace: dict[str | None, tuple[Decimal, int, set[str]]] = defaultdict(
        lambda: (Decimal("0"), 0, set())
    )

    for row in rows:
        ts = row.timestamp.dt
        cost = row.cost_usd
        total_7d += cost
        n_7d += 1
        day_key = ts.date().isoformat()
        prev_cost, prev_count = by_day[day_key]
        by_day[day_key] = (prev_cost + cost, prev_count + 1)
        if ts >= cutoff_24h:
            total_24h += cost
            n_24h += 1
            cached_24h += row.tokens_cache_read
            group_key = f"{row.provider} / {row.role}"
            prev_cost_g, prev_count_g = by_group[group_key]
            by_group[group_key] = (prev_cost_g + cost, prev_count_g + 1)
            prev_cost_t, prev_count_t, roles = by_trace[row.trace_id]
            roles.add(row.role)
            by_trace[row.trace_id] = (prev_cost_t + cost, prev_count_t + 1, roles)

    per_day = tuple(
        DayRollup(day=day, cost_usd=cost, call_count=count)
        for day, (cost, count) in sorted(by_day.items(), reverse=True)
    )
    per_group = tuple(
        sorted(
            (
                GroupRollup(key=k, cost_usd=cost, call_count=count)
                for k, (cost, count) in by_group.items()
            ),
            key=lambda g: g.cost_usd,
            reverse=True,
        )
    )
    # P4.4a's trace clock groups calls by advisory evaluation. Traced
    # groups sort by cost (capped — one row per evaluation adds up);
    # the NULL bucket renders last as one aggregate "untraced" row.
    traced = sorted(
        (
            TraceRollup(
                trace_label=trace[:8],
                cost_usd=cost,
                call_count=count,
                roles=", ".join(sorted(roles)),
            )
            for trace, (cost, count, roles) in by_trace.items()
            if trace is not None
        ),
        key=lambda t: t.cost_usd,
        reverse=True,
    )[:_TRACE_ROWS_CAP]
    per_trace = tuple(traced)
    if None in by_trace:
        cost, count, roles = by_trace[None]
        per_trace = (
            *per_trace,
            TraceRollup(
                trace_label="untraced",
                cost_usd=cost,
                call_count=count,
                roles=", ".join(sorted(roles)),
            ),
        )
    return CostSnapshot(
        total_24h_usd=total_24h,
        total_7d_usd=total_7d,
        call_count_24h=n_24h,
        call_count_7d=n_7d,
        cached_tokens_24h=cached_24h,
        per_day=per_day,
        per_provider_role=per_group,
        per_trace=per_trace,
    )


async def _load_snapshot(storage: StoragePort) -> CostSnapshot:
    """Pull the last 7 days from operator.db and roll up.

    Storage failure degrades to an empty snapshot with an error
    string — the dashboard renders with a single error card rather
    than 500-ing.
    """
    now = datetime.now(UTC)
    since = Timestamp(dt=now - timedelta(days=7))
    try:
        rows = await storage.get_llm_calls(since=since)
    except StorageError as exc:
        return _empty_snapshot(error=f"failed to query llm_calls: {exc}")
    return _rollup(rows, now=now)


async def _load_honesty_snapshot(
    storage: StoragePort,
    live_storage: StoragePort | None,
    *,
    monthly_infra_usd: Decimal | None,
) -> CostHonestySnapshot:
    """Trades → cycles, plus 30d of llm_calls → the honest ledger.

    Re-fetches trades and llm_calls rather than threading them from
    the sibling loaders — two extra indexed reads of a few thousand
    rows per page load, in exchange for three loaders that stay
    independently testable and independently degradable.
    """
    if live_storage is None:
        return _empty_honesty_snapshot(wired=False)
    now = datetime.now(UTC)
    try:
        trades = await live_storage.get_trades(limit=10_000)
    except StorageError as exc:
        return _empty_honesty_snapshot(wired=True, error=f"failed to query trades: {exc}")
    try:
        llm_rows = await storage.get_llm_calls(since=Timestamp(dt=now - timedelta(days=30)))
    except StorageError as exc:
        return _empty_honesty_snapshot(wired=True, error=f"failed to query llm_calls: {exc}")
    # get_trades returns newest-first; the matcher wants chronological.
    cycles = match_cycles(list(reversed(trades)))
    return _rollup_honesty(cycles, llm_rows, now=now, monthly_infra_usd=monthly_infra_usd)


# --------------------------------------------------------------------- #
# Routes                                                                #
# --------------------------------------------------------------------- #


@router.get("/cost", response_class=HTMLResponse)
async def cost_page(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    live_storage: StoragePort | None = Depends(get_live_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
    config: WebConfig = Depends(get_config),
) -> Response:
    """Full cost dashboard page — honesty + LLM + trading-fees cards."""
    snapshot = await _load_snapshot(storage)
    fees_snapshot = await _load_trading_fees_snapshot(live_storage)
    honesty_snapshot = await _load_honesty_snapshot(
        storage, live_storage, monthly_infra_usd=config.cost_assumptions.monthly_infra_usd
    )
    return templates.TemplateResponse(
        request,
        "cost.html",
        {
            "snapshot": snapshot,
            "fees_snapshot": fees_snapshot,
            "honesty_snapshot": honesty_snapshot,
            "username": user.username,
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
        },
    )


@router.get("/cost/card", response_class=HTMLResponse)
async def cost_card(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    storage: StoragePort = Depends(get_operator_storage),
    live_storage: StoragePort | None = Depends(get_live_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
    config: WebConfig = Depends(get_config),
) -> Response:
    """HTMX fragment — LLM cost + trading-fees cards without chrome.

    The dashboard's HTMX polling hits this endpoint; all cards
    refresh together to keep the visual state consistent.
    """
    snapshot = await _load_snapshot(storage)
    fees_snapshot = await _load_trading_fees_snapshot(live_storage)
    honesty_snapshot = await _load_honesty_snapshot(
        storage, live_storage, monthly_infra_usd=config.cost_assumptions.monthly_infra_usd
    )
    return templates.TemplateResponse(
        request,
        "_cost_card.html",
        {
            "snapshot": snapshot,
            "fees_snapshot": fees_snapshot,
            "honesty_snapshot": honesty_snapshot,
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
        },
    )


__all__ = (
    "router",
    "CostHonestySnapshot",
    "CostSnapshot",
    "DayRollup",
    "GroupRollup",
    "HonestyWindow",
    "TraceRollup",
    "TradingFeesSnapshot",
)

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

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.models import Order, Trade
from wobblebot.domain.users import User
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import (
    get_live_storage,
    get_observe_storage,
    get_operator_storage,
    get_templates,
)

_LOGGER = logging.getLogger(__name__)
_PRICE_LOOKBACK_MINUTES = 5

router = APIRouter(tags=["status"])


@dataclass(frozen=True)
class StatusSnapshot:
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
    error: str | None = None


def _empty_snapshot(*, wired: bool, error: str | None = None) -> StatusSnapshot:
    return StatusSnapshot(
        live_wired=wired,
        open_orders=(),
        recent_trades=(),
        last_fill_age_seconds=None,
        error=error,
    )


async def _load_current_prices(
    observe_storage: StoragePort | None,
    symbols: set[Symbol],
) -> dict[Symbol, Decimal]:
    """Best-effort fetch of latest price per symbol from observe.db.

    Returns ``{}`` if observe.db is unwired. Per-symbol failures are
    logged + skipped rather than raised — a missing price is fine
    to display as a dash; raising would 500 the whole status card.
    """
    if observe_storage is None or not symbols:
        return {}
    cutoff = datetime.now(UTC) - timedelta(minutes=_PRICE_LOOKBACK_MINUTES)
    prices: dict[Symbol, Decimal] = {}
    for symbol in symbols:
        try:
            snapshots = await observe_storage.get_price_snapshots(
                symbol=symbol, start_time=cutoff
            )
        except StorageError as exc:
            _LOGGER.warning(
                "current-price lookup failed for %s; skipping",
                symbol,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        if not snapshots:
            continue
        latest = max(snapshots, key=lambda s: s.observed_at.dt)
        prices[symbol] = latest.price.amount
    return prices


async def _load_snapshot(
    live_storage: StoragePort | None,
    observe_storage: StoragePort | None,
) -> StatusSnapshot:
    """Pull open orders + recent fills + current prices; degrade gracefully."""
    if live_storage is None:
        return _empty_snapshot(wired=False)
    try:
        open_orders = await live_storage.get_open_orders()
        recent = await live_storage.get_trades(limit=20)
    except StorageError as exc:
        return _empty_snapshot(wired=True, error=f"failed to query live.db: {exc}")
    last_age: float | None = None
    if recent:
        most_recent = max(recent, key=lambda t: t.executed_at.dt)
        delta = datetime.now(UTC) - most_recent.executed_at.dt
        last_age = delta.total_seconds()
    symbols_with_orders = {o.symbol for o in open_orders}
    prices = await _load_current_prices(observe_storage, symbols_with_orders)
    return StatusSnapshot(
        live_wired=True,
        open_orders=tuple(open_orders),
        recent_trades=tuple(recent),
        last_fill_age_seconds=last_age,
        current_prices=prices,
    )


# --------------------------------------------------------------------- #
# Dashboard root replaces the 7.1 stub                                  #
# --------------------------------------------------------------------- #


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    live_storage: StoragePort | None = Depends(get_live_storage),
    observe_storage: StoragePort | None = Depends(get_observe_storage),
    operator_storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Combined dashboard — cost card + open orders + recent fills."""
    snapshot = await _load_snapshot(live_storage, observe_storage)
    assert user.id is not None
    prefs = await operator_storage.get_user_preferences(user.id)
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
async def status_card(
    request: Request,
    user: User = Depends(require_user),
    live_storage: StoragePort | None = Depends(get_live_storage),
    observe_storage: StoragePort | None = Depends(get_observe_storage),
    operator_storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """HTMX fragment — open-orders + recent-fills card without chrome."""
    snapshot = await _load_snapshot(live_storage, observe_storage)
    assert user.id is not None
    prefs = await operator_storage.get_user_preferences(user.id)
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

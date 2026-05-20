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

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.models import Order, Trade
from wobblebot.domain.users import User
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import (
    get_live_storage,
    get_operator_storage,
    get_templates,
)

router = APIRouter(tags=["status"])


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything the status template needs in one immutable bundle."""

    live_wired: bool
    open_orders: tuple[Order, ...]
    recent_trades: tuple[Trade, ...]
    last_fill_age_seconds: float | None
    error: str | None = None


def _empty_snapshot(*, wired: bool, error: str | None = None) -> StatusSnapshot:
    return StatusSnapshot(
        live_wired=wired,
        open_orders=(),
        recent_trades=(),
        last_fill_age_seconds=None,
        error=error,
    )


async def _load_snapshot(
    live_storage: StoragePort | None,
) -> StatusSnapshot:
    """Pull open orders + recent fills; degrade gracefully on failure."""
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
    return StatusSnapshot(
        live_wired=True,
        open_orders=tuple(open_orders),
        recent_trades=tuple(recent),
        last_fill_age_seconds=last_age,
    )


# --------------------------------------------------------------------- #
# Dashboard root replaces the 7.1 stub                                  #
# --------------------------------------------------------------------- #


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    live_storage: StoragePort | None = Depends(get_live_storage),
    operator_storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Combined dashboard — cost card + open orders + recent fills."""
    snapshot = await _load_snapshot(live_storage)
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
    operator_storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """HTMX fragment — open-orders + recent-fills card without chrome."""
    snapshot = await _load_snapshot(live_storage)
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

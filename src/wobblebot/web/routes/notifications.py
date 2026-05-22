"""Notifications page + bell-badge endpoint (Stage 8.4.E soak Day 4).

Reads the ``notifications`` table that ``cli/live`` + ``cli/harvest``
write to via ``SqliteNotifierAdapter`` (Phase 5.5). This route is a
parallel consumer of that pipeline alongside ``cli/operator``'s
Discord forwarder — both surfaces show the same notifications, both
can be running concurrently without fighting.

v1.0 ships info-only:
- ``GET /notifications`` renders the full list (last 100).
- ``GET /notifications/latest-timestamp`` returns JSON for the
  bell-badge polling logic in ``layout.html`` (browser-local
  ``last_seen`` timestamp in localStorage; no schema migration).

v1.1 candidates (logged in v1.0-future-improvements.md):
- Server-side ``read_at`` column + acknowledge endpoints
- Deep-link per-notification-type (e.g. fill -> /dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse, Response

from wobblebot.domain.users import User
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notifier import PersistedNotification
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import (
    get_operator_storage,
    get_templates,
)

_NOTIFICATIONS_LIMIT = 100

router = APIRouter(tags=["notifications"])


@dataclass(frozen=True)
class NotificationsSnapshot:
    """List of recent notifications + an error placeholder for the page."""

    notifications: tuple[PersistedNotification, ...] = field(default_factory=tuple)
    error: str | None = None


async def _load_snapshot(operator_storage: StoragePort) -> NotificationsSnapshot:
    """Pull recent notifications; degrade gracefully on storage failure."""
    try:
        rows = await operator_storage.get_notifications(forwarded=None, limit=_NOTIFICATIONS_LIMIT)
    except StorageError as exc:
        return NotificationsSnapshot(error=f"failed to query notifications: {exc}")
    # Newest first for the page.
    sorted_rows = sorted(rows, key=lambda n: n.created_at.dt, reverse=True)
    return NotificationsSnapshot(notifications=tuple(sorted_rows))


@router.get("/notifications", response_class=HTMLResponse)
async def notifications(
    request: Request,
    user: User = Depends(require_user),
    operator_storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Render the notifications list."""
    snapshot = await _load_snapshot(operator_storage)
    assert user.id is not None
    prefs = await operator_storage.get_user_preferences(user.id)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


@router.get("/notifications/latest-timestamp", response_class=JSONResponse)
async def notifications_latest_timestamp(
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    operator_storage: StoragePort = Depends(get_operator_storage),
) -> JSONResponse:
    """Return the ISO timestamp of the most recent notification.

    Polled by ``layout.html``'s bell-badge JS every 30s. Returns
    ``{"latest_at": null}`` when there are no notifications yet.
    Cheap query (single row by index); fine to poll without
    introducing a server-side cache.
    """
    try:
        rows = await operator_storage.get_notifications(forwarded=None, limit=1)
    except StorageError:
        # Don't 500 the badge polling; behave like there are no notifications.
        return JSONResponse({"latest_at": None})
    if not rows:
        return JSONResponse({"latest_at": None})
    latest: datetime = max(r.created_at.dt for r in rows)
    return JSONResponse({"latest_at": latest.isoformat()})


__all__ = ("router", "NotificationsSnapshot")

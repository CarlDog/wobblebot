"""Notifications page, read-state actions, and bell-badge endpoint.

Reads the ``notifications`` table that ``cli/live`` + ``cli/harvest``
write to via ``SqliteNotifierAdapter`` (Phase 5.5). This route is a
parallel consumer of that pipeline alongside ``cli/operator``'s
Discord forwarder — both surfaces show the same notifications, both
can be running concurrently without fighting.

**P3 slice 19 — server-side read-state.** v1.0's bell badge compared
the newest row's timestamp against a browser-local ``last_seen``
value, so clearing the dot on the desktop left the phone still
dotted, and merely *visiting* this page counted as reading
everything. Both are now server-side: ``notifications.read_at``
carries the operator's acknowledgement, the badge polls a real
unread count, and dismissing is an explicit action (per row, or
"Mark all read"). Acknowledgement is deliberately NOT implicit on
page visit — a badge you must dismiss is the point of a badge.

These writes are **UI-local**, not ``pending_commands`` round-trips,
following the ``reanchor_snoozes`` precedent (P3 slice 5): reading a
notification moves no money and touches no engine state, so the
ADR-002 firewall doesn't apply. Auth + CSRF still gate them like
every other mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette import status
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notification_events import (
    CommandResultEvent,
    FillEvent,
    HarvestProposalEvent,
    LossCapEvent,
    NotificationEvent,
    SessionEndEvent,
    SessionStartEvent,
    WithdrawalFailedEvent,
    WithdrawalSubmittedEvent,
)
from wobblebot.ports.notifier import PersistedNotification
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_operator_storage,
    get_templates,
)
from wobblebot.web.middleware import require_csrf_token

_NOTIFICATIONS_LIMIT = 100

router = APIRouter(tags=["notifications"])


def deep_link(event: NotificationEvent | None) -> str | None:
    """Map a typed notification event to the page that explains it.

    Only the typed rows (P3 slice 7's union) get a link — a legacy or
    deliberately-generic row has no reliable destination, and a link
    that lands somewhere unhelpful is worse than no link.

    The two destinations mirror the two things the operator does
    about a notification: trading events send them to the dashboard,
    treasury events to the harvester page where the proposal and its
    Execute button live.
    """
    match event:
        case (
            SessionStartEvent()
            | SessionEndEvent()
            | LossCapEvent()
            | FillEvent()
            | CommandResultEvent()
        ):
            return "/"
        case HarvestProposalEvent() | WithdrawalSubmittedEvent() | WithdrawalFailedEvent():
            return "/harvester"
        case _:
            return None


@dataclass(frozen=True)
class NotificationView:
    """One table row: the record plus its resolved deep link."""

    row: PersistedNotification
    link: str | None


@dataclass(frozen=True)
class NotificationsSnapshot:
    """List of recent notifications + unread count + error placeholder."""

    notifications: tuple[NotificationView, ...] = field(default_factory=tuple)
    unread: int = 0
    error: str | None = None


async def _load_snapshot(operator_storage: StoragePort) -> NotificationsSnapshot:
    """Pull recent notifications; degrade gracefully on storage failure."""
    try:
        rows = await operator_storage.get_notifications(forwarded=None, limit=_NOTIFICATIONS_LIMIT)
        unread = await operator_storage.count_unread_notifications()
    except StorageError as exc:
        return NotificationsSnapshot(error=f"failed to query notifications: {exc}")
    # The port returns newest-first, so the rows are already in page order
    # and the limit keeps the newest 100 (not the oldest 100, which would
    # freeze this page once the table outgrows the limit).
    return NotificationsSnapshot(
        notifications=tuple(
            NotificationView(row=row, link=deep_link(row.notification.event)) for row in rows
        ),
        unread=unread,
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications(
    request: Request,
    user: User = Depends(require_user),
    operator_storage: StoragePort = Depends(get_operator_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Render the notifications list."""
    snapshot = await _load_snapshot(operator_storage)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: int,
    _csrf: None = Depends(require_csrf_token),
    _user: User = Depends(require_user),
    operator_storage: StoragePort = Depends(get_operator_storage),
) -> Response:
    """Acknowledge one notification (the per-row "Acknowledge" button).

    Silent on a miss: an id that's already read, or pruned by the
    maintenance daemon between render and click, is not an error
    worth showing — the operator's intent (make this stop nagging me)
    is satisfied either way.
    """
    try:
        await operator_storage.mark_notifications_read(
            [notification_id], Timestamp(dt=datetime.now(UTC))
        )
    except StorageError as exc:
        return HTMLResponse(f"Failed to mark notification read: {exc}", status_code=500)
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/notifications/read-all")
async def mark_all_read(
    _csrf: None = Depends(require_csrf_token),
    _user: User = Depends(require_user),
    operator_storage: StoragePort = Depends(get_operator_storage),
) -> Response:
    """Acknowledge every unread notification ("Mark all read")."""
    try:
        await operator_storage.mark_all_notifications_read(Timestamp(dt=datetime.now(UTC)))
    except StorageError as exc:
        return HTMLResponse(f"Failed to mark notifications read: {exc}", status_code=500)
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/notifications/latest-timestamp", response_class=JSONResponse)
async def notifications_latest_timestamp(
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    operator_storage: StoragePort = Depends(get_operator_storage),
) -> JSONResponse:
    """Return the unread count + newest notification timestamp.

    Polled by ``nav.js``'s bell-badge logic every 30s. ``unread``
    drives the dot (server-side, so it agrees across devices);
    ``latest_at`` is kept for the timestamp display and because a
    cached older ``nav.js`` still reads it. Cheap query — the unread
    count rides the partial index — so it's fine to poll without a
    server-side cache.
    """
    try:
        rows = await operator_storage.get_notifications(forwarded=None, limit=1)
        unread = await operator_storage.count_unread_notifications()
    except StorageError:
        # Don't 500 the badge polling; behave like there are no notifications.
        return JSONResponse({"latest_at": None, "unread": 0})
    latest: datetime | None = max((r.created_at.dt for r in rows), default=None)
    return JSONResponse({"latest_at": latest.isoformat() if latest else None, "unread": unread})


__all__ = ("router", "NotificationsSnapshot", "NotificationView", "deep_link")

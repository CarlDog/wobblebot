"""Audit view — reads pending_commands + notifications (Stage 7.4.B).

The audit log answers "what has the operator (and the bot) done, and
with what outcome." Two tables side-by-side:

- **pending_commands** — every mutation request (Discord + web) with
  its lifecycle state. The ADR-013 firewall's forensic record.
- **notifications** — every outbound event cli/live + cli/harvest
  emitted, with forwarded-to-Discord flag.

Both live in ``operator.db`` (required, never unwired). The Stage
7.1.D stub /audit route in pages.py is superseded here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.notifier import PersistedNotification
from wobblebot.ports.operator import PendingCommand
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_operator_storage, get_templates

router = APIRouter(tags=["audit"])

# Display the most-recent N entries per table; pull a wider slice
# for the total count. Audit log grows ~5/day during normal soak;
# 1000 row cap covers many weeks.
_AUDIT_DISPLAY_LIMIT = 50
_AUDIT_QUERY_LIMIT = 1000


@dataclass(frozen=True)
class AuditSnapshot:
    pending_commands: tuple[PendingCommand, ...]
    notifications: tuple[PersistedNotification, ...]
    pending_total: int = 0
    notifications_total: int = 0
    error: str | None = None


async def _load_snapshot(storage: StoragePort) -> AuditSnapshot:
    try:
        # pending_commands: all rows, oldest-first per the port contract;
        # we display newest-first by reversing then capping.
        pending = await storage.get_pending_commands(limit=_AUDIT_QUERY_LIMIT)
        notifications = await storage.get_notifications(limit=_AUDIT_QUERY_LIMIT)
    except StorageError as exc:
        return AuditSnapshot(
            pending_commands=(),
            notifications=(),
            error=f"failed to query operator.db: {exc}",
        )
    pending_newest_first = list(reversed(pending))
    notifications_newest_first = list(reversed(notifications))
    return AuditSnapshot(
        pending_commands=tuple(pending_newest_first[:_AUDIT_DISPLAY_LIMIT]),
        notifications=tuple(notifications_newest_first[:_AUDIT_DISPLAY_LIMIT]),
        pending_total=len(pending),
        notifications_total=len(notifications),
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    snapshot = await _load_snapshot(storage)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"snapshot": snapshot, "username": user.username},
    )


__all__ = ("router", "AuditSnapshot")

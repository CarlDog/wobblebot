"""Mutation routes — pause / resume / stop via the ADR-013 firewall (Stage 7.2.C).

Architecturally significant: the web UI is the second writer to
``operator.db``'s ``pending_commands`` table (cli/operator was the
first; ADR-013). The flow is:

1. ``GET /commands/<verb>`` — render a form prompting for the symbol
   (or just a confirmation button, for ``stop``).
2. ``POST /commands/<verb>`` — write a ``PendingCommand`` row with
   ``status="awaiting_confirmation"`` and redirect to the confirm
   page.
3. ``GET /commands/<id>/confirm`` — summarize the pending command +
   show approve / reject buttons.
4. ``POST /commands/<id>/confirm`` — transition the row to
   ``approved`` or ``rejected`` based on which button.

cli/live's ``WHERE status='approved'`` poll picks the row up on the
next tick and dispatches it. **The web UI never calls
OperatorService.dispatch_command directly** — every state mutation
crosses the pending_commands table so the ADR-002 firewall stays
the single source of truth for "intent → engine".

CSRF protection: every POST is gated by ``require_csrf_token`` (the
same dependency the auth routes use). The form templates emit the
hidden ``csrf_token`` input via the ``csrf_input`` Jinja2 global.

``channel_id`` is set to the literal ``"web"`` so audit-log
inspection can distinguish web-originated commands from Discord-
originated ones.
"""

# pylint: disable=too-many-arguments,too-many-positional-arguments
# FastAPI's Depends-based DI naturally produces handlers with many
# parameters; the pattern is canonical and not a code smell.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.operator import (
    OperatorCommand,
    PauseCommand,
    PendingCommand,
    ResumeCommand,
    StopCommand,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import get_operator_storage, get_templates
from wobblebot.web.middleware import require_csrf_token

router = APIRouter(prefix="/commands", tags=["commands"])


# Web UI commands get a fixed 10-minute TTL — long enough for the
# operator to step away mid-flow and come back, short enough that
# abandoned approvals don't accumulate. The TTL expirer in
# cli/operator (Stage 5.7) reaps any awaiting_confirmation rows that
# pass their TTL.
_WEB_TTL_MINUTES = 10
_WEB_CHANNEL_ID = "web"


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #


def _parse_symbol(raw: str) -> Symbol:
    """Validate ``BTC/USD``-style symbol input from a form."""
    return Symbol.from_string(raw.strip())


async def _create_pending(
    *,
    command: OperatorCommand,
    user: User,
    storage: StoragePort,
) -> PendingCommand:
    """Persist a fresh awaiting-confirmation pending command."""
    now = Timestamp(dt=datetime.now(UTC))
    pending = PendingCommand(
        id=uuid4(),
        command=command,
        status="awaiting_confirmation",
        channel_id=_WEB_CHANNEL_ID,
        requesting_user_id=user.username,
        ttl_expires_at=Timestamp(dt=now.dt + timedelta(minutes=_WEB_TTL_MINUTES)),
        created_at=now,
    )
    await storage.save_pending_command(pending)
    return pending


# --------------------------------------------------------------------- #
# GET forms                                                             #
# --------------------------------------------------------------------- #


@router.get("/pause", response_class=HTMLResponse)
async def pause_form(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    return templates.TemplateResponse(
        request,
        "command_form.html",
        {
            "page_title": "Pause symbol",
            "verb": "pause",
            "verb_label": "Pause",
            "form_action": "/commands/pause",
            "username": user.username,
            "needs_symbol": True,
            "error": None,
        },
    )


@router.get("/resume", response_class=HTMLResponse)
async def resume_form(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    return templates.TemplateResponse(
        request,
        "command_form.html",
        {
            "page_title": "Resume symbol",
            "verb": "resume",
            "verb_label": "Resume",
            "form_action": "/commands/resume",
            "username": user.username,
            "needs_symbol": True,
            "error": None,
        },
    )


@router.get("/stop", response_class=HTMLResponse)
async def stop_form(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    return templates.TemplateResponse(
        request,
        "command_form.html",
        {
            "page_title": "Emergency stop",
            "verb": "stop",
            "verb_label": "Emergency stop",
            "form_action": "/commands/stop",
            "username": user.username,
            "needs_symbol": False,
            "error": None,
        },
    )


# --------------------------------------------------------------------- #
# POST creates                                                          #
# --------------------------------------------------------------------- #


def _redirect_to_confirm(pending_id: UUID) -> Response:
    return RedirectResponse(
        url=f"/commands/{pending_id}/confirm",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/pause")
async def pause_submit(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
    symbol: str = Form(..., min_length=1, max_length=32),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    try:
        parsed = _parse_symbol(symbol)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "command_form.html",
            {
                "page_title": "Pause symbol",
                "verb": "pause",
                "verb_label": "Pause",
                "form_action": "/commands/pause",
                "username": user.username,
                "needs_symbol": True,
                "error": f"Invalid symbol: {exc}",
            },
            status_code=400,
        )
    pending = await _create_pending(
        command=PauseCommand(symbol=parsed),
        user=user,
        storage=storage,
    )
    return _redirect_to_confirm(pending.id)


@router.post("/resume")
async def resume_submit(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
    symbol: str = Form(..., min_length=1, max_length=32),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    try:
        parsed = _parse_symbol(symbol)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "command_form.html",
            {
                "page_title": "Resume symbol",
                "verb": "resume",
                "verb_label": "Resume",
                "form_action": "/commands/resume",
                "username": user.username,
                "needs_symbol": True,
                "error": f"Invalid symbol: {exc}",
            },
            status_code=400,
        )
    pending = await _create_pending(
        command=ResumeCommand(symbol=parsed),
        user=user,
        storage=storage,
    )
    return _redirect_to_confirm(pending.id)


@router.post("/stop")
async def stop_submit(
    _request: Request,
    _csrf: None = Depends(require_csrf_token),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
) -> Response:
    pending = await _create_pending(
        command=StopCommand(),
        user=user,
        storage=storage,
    )
    return _redirect_to_confirm(pending.id)


# --------------------------------------------------------------------- #
# Confirm flow                                                          #
# --------------------------------------------------------------------- #


@router.get("/{pending_id}/confirm", response_class=HTMLResponse)
async def confirm_form(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    pending_id: UUID,
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    pending = await storage.get_pending_command(pending_id)
    if pending is None:
        return templates.TemplateResponse(
            request,
            "command_missing.html",
            {"pending_id": str(pending_id), "username": user.username},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "command_confirm.html",
        {
            "pending": pending,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


@router.post("/{pending_id}/confirm")
async def confirm_submit(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    pending_id: UUID,
    _csrf: None = Depends(require_csrf_token),
    decision: str = Form(..., pattern="^(approve|reject)$"),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    pending = await storage.get_pending_command(pending_id)
    if pending is None:
        return templates.TemplateResponse(
            request,
            "command_missing.html",
            {"pending_id": str(pending_id), "username": user.username},
            status_code=404,
        )
    # Idempotency: ignore if not in awaiting_confirmation. The original
    # operator may have approved/rejected via Discord in parallel.
    if pending.status != "awaiting_confirmation":
        return templates.TemplateResponse(
            request,
            "command_result.html",
            {
                "pending": pending,
                "username": user.username,
                "already": True,
                "operator_tz": prefs.timezone,
            },
        )

    now = Timestamp(dt=datetime.now(UTC))
    new_status = "approved" if decision == "approve" else "rejected"
    updated = pending.model_copy(
        update={
            "status": new_status,
            "confirming_user_id": user.username,
            "confirmed_at": now,
        }
    )
    try:
        await storage.save_pending_command(updated)
    except StorageError as exc:
        return templates.TemplateResponse(
            request,
            "command_result.html",
            {
                "pending": pending,
                "username": user.username,
                "already": False,
                "error": f"failed to persist transition: {exc}",
                "operator_tz": prefs.timezone,
            },
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "command_result.html",
        {
            "pending": updated,
            "username": user.username,
            "already": False,
            "operator_tz": prefs.timezone,
        },
    )


__all__ = ("router",)

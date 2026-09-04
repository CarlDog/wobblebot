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
    ExecuteProposalCommand,
    PauseCommand,
    PendingCommand,
    QueueableCommand,
    ReanchorCommand,
    ResumeCommand,
    StopCommand,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_harvest_storage,
    get_live_symbols,
    get_operator_storage,
    get_templates,
    get_withdrawal_destinations,
)
from wobblebot.web.middleware import require_csrf_token

router = APIRouter(prefix="/commands", tags=["commands"])


# Web UI commands get a fixed 10-minute TTL — long enough for the
# operator to step away mid-flow and come back, short enough that
# abandoned approvals don't accumulate. The TTL expirer in
# cli/operator (Stage 5.7) reaps any awaiting_confirmation rows that
# pass their TTL.
_WEB_TTL_MINUTES = 10
_WEB_CHANNEL_ID = "web"
# Banner snooze horizon ("Snooze 24h" per the P3 blueprint). Fixed —
# a duration picker is speculative surface until an operator asks.
_SNOOZE_HOURS = 24
# Proposal lookup slice for the Execute button. Matches cli/harvest's own
# limit so the web can't offer a proposal the daemon then can't find.
_PROPOSAL_LOOKUP_LIMIT = 1000


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #


def _is_htmx(request: Request) -> bool:
    """True when the request came from htmx (the modal flow).

    Non-htmx (no-JS) requests keep the original full-page redirect
    flow — the modal is pure progressive enhancement.
    """
    return request.headers.get("HX-Request") == "true"


def _parse_symbol(raw: str) -> Symbol:
    """Validate ``BTC/USD``-style symbol input from a form."""
    return Symbol.from_string(raw.strip())


async def _create_pending(
    *,
    command: QueueableCommand,
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
    prefs: UserPreferences = Depends(get_user_preferences),
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
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_confirm.html",
            {"pending": pending, "operator_tz": prefs.timezone},
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
    prefs: UserPreferences = Depends(get_user_preferences),
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
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_confirm.html",
            {"pending": pending, "operator_tz": prefs.timezone},
        )
    return _redirect_to_confirm(pending.id)


@router.post("/stop")
async def stop_submit(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    pending = await _create_pending(
        command=StopCommand(),
        user=user,
        storage=storage,
    )
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_confirm.html",
            {"pending": pending, "operator_tz": prefs.timezone},
        )
    return _redirect_to_confirm(pending.id)


@router.post("/reanchor")
async def reanchor_submit(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
    symbol: str = Form(..., min_length=1, max_length=32),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
    prefs: UserPreferences = Depends(get_user_preferences),
    configured: frozenset[Symbol] = Depends(get_live_symbols),
) -> Response:
    """Banner "Re-anchor" button (ADR-031, P3 banner slice).

    Same firewall shape as pause/resume: a ``ReanchorCommand`` row in
    ``awaiting_confirmation``, then the shared confirm page. The
    destructive part (cancel + fresh anchor + re-lay) only runs after
    the operator approves AND cli/live's ``status='approved'`` poll
    picks it up — the button itself moves nothing.
    """
    try:
        parsed = _parse_symbol(symbol)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "command_form.html",
            {
                "page_title": "Re-anchor symbol",
                "verb": "reanchor",
                "verb_label": "Re-anchor",
                "form_action": "/commands/reanchor",
                "username": user.username,
                "needs_symbol": True,
                "error": f"Invalid symbol: {exc}",
            },
            status_code=400,
        )
    # 2026-09-03 review, finding 2. Symbol cards render for any asset with
    # a held balance, which is WIDER than the engine's trading set, and the
    # per-card anchor button made an untraded symbol one click from a real
    # layout: for_coin() hands any unknown base a default config, the sell
    # guard passes it unguarded for want of a cost basis, cli/live never
    # ticks it, and _cancel_all_open skips it on clean shutdown — orders
    # left live on Kraken indefinitely. Enforced HERE rather than in the
    # template so the free-text form and any future caller are covered too.
    # Empty ``configured`` means unknown (no live: section) and falls open.
    if configured and parsed not in configured:
        return templates.TemplateResponse(
            request,
            "command_form.html",
            {
                "page_title": "Re-anchor symbol",
                "verb": "reanchor",
                "verb_label": "Re-anchor",
                "form_action": "/commands/reanchor",
                "username": user.username,
                "needs_symbol": True,
                "error": (
                    f"{parsed} is not one of the engine's configured trading symbols, "
                    "so cli/live would never tend a grid placed on it. Add it to "
                    "live.symbols first if you mean to trade it."
                ),
            },
            status_code=400,
        )
    pending = await _create_pending(
        command=ReanchorCommand(symbol=parsed),
        user=user,
        storage=storage,
    )
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_confirm.html",
            {"pending": pending, "operator_tz": prefs.timezone},
        )
    return _redirect_to_confirm(pending.id)


@router.post("/execute-proposal")
async def execute_proposal_submit(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
    proposal_id: str = Form(..., min_length=1, max_length=128),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
    prefs: UserPreferences = Depends(get_user_preferences),
    harvest_storage: StoragePort | None = Depends(get_harvest_storage),
    destinations: dict[str, str] = Depends(get_withdrawal_destinations),
) -> Response:
    """Harvester "Execute" button — queue a withdrawal for approval (ADR-034).

    The web never withdraws. This writes an ``ExecuteProposalCommand``
    row in ``awaiting_confirmation``; only after the operator approves
    does ``cli/harvest``'s kind-scoped poll pick it up and run the seven
    defense layers. ADR-003 is intact — the web process has no
    withdraw-scoped key and calls no exchange API.

    The amount and destination are read **server-side** from the stored
    proposal + config, never from the form: a client-supplied amount
    would make the echo-validation gate meaningless (the browser could
    then name both what it approves and what it checks against). The
    form carries only the proposal id.
    """
    if harvest_storage is None:
        return HTMLResponse("Harvest storage is not wired.", status_code=503)
    try:
        proposals = await harvest_storage.get_transfer_proposals(limit=_PROPOSAL_LOOKUP_LIMIT)
    except StorageError as exc:
        return HTMLResponse(f"Failed to read proposals: {exc}", status_code=503)
    proposal = next((p for p in proposals if p.proposal_id == proposal_id), None)
    if proposal is None:
        return HTMLResponse("Proposal not found.", status_code=404)
    destination = destinations.get(proposal.asset)
    if not destination:
        return HTMLResponse(
            f"No withdrawal destination configured for {proposal.asset}.",
            status_code=400,
        )
    pending = await _create_pending(
        command=ExecuteProposalCommand(
            proposal_id=proposal.proposal_id,
            amount_usd=proposal.amount,
            destination=destination,
        ),
        user=user,
        storage=storage,
    )
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_confirm.html",
            {"pending": pending, "operator_tz": prefs.timezone, "proposal": proposal},
        )
    return _redirect_to_confirm(pending.id)


@router.post("/snooze-reanchor")
async def snooze_reanchor_submit(
    _request: Request,
    _csrf: None = Depends(require_csrf_token),
    symbol: str = Form(..., min_length=1, max_length=32),
    _user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
) -> Response:
    """Banner "Snooze 24h" button — deliberately UI-local (P3 blueprint).

    Writes the ``reanchor_snoozes`` row directly instead of a
    ``pending_commands`` round-trip: snoozing a banner moves no money
    and touches no engine state, so the ADR-002 firewall doesn't
    apply. Still auth + CSRF gated like every other mutation.
    """
    try:
        parsed = _parse_symbol(symbol)
    except ValueError:
        return HTMLResponse("Invalid symbol", status_code=400)
    snoozed_until = datetime.now(UTC) + timedelta(hours=_SNOOZE_HOURS)
    await storage.save_reanchor_snooze(parsed, snoozed_until)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/hide-symbol")
async def hide_symbol_submit(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    _request: Request,
    _csrf: None = Depends(require_csrf_token),
    symbol: str = Form(..., min_length=1, max_length=32),
    hidden: bool = Form(True),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    configured: frozenset[Symbol] = Depends(get_live_symbols),
) -> Response:
    """Eye toggle on a symbol card — deliberately UI-local, like Snooze.

    Writes ``hidden_symbols`` directly rather than through
    ``pending_commands``: hiding a card moves no money and touches no
    engine state, so the ADR-002 firewall does not apply. Still auth +
    CSRF gated like every other mutation. Per user, because two
    operators must not fight over one visibility list.

    **A symbol the engine trades cannot be hidden.** Pause, resume and
    re-anchor exist only inside the card, so hiding a configured symbol
    would silently un-ship the anchor button added in 2.0.4 and leave no
    surface to stop a symbol that is trading. The motivating case needs
    none of that: BABY/USD is not in ``live.symbols`` and was never
    traded — its card comes from a dust balance in the observe snapshot.

    Enforced HERE and not only in the template, so a hand-rolled POST is
    covered too. Empty ``configured`` means unknown (no ``live:``
    section) and falls open, matching ``reanchor_submit``.
    """
    try:
        parsed = _parse_symbol(symbol)
    except ValueError:
        return HTMLResponse("Invalid symbol", status_code=400)
    if hidden and parsed in configured:
        return HTMLResponse(
            f"{parsed} is traded by the engine and cannot be hidden: "
            "its card carries the pause, resume and re-anchor controls.",
            status_code=400,
        )
    # Same assert as settings.py / auth.get_user_preferences: a user that
    # reached require_user is persisted, so id is set.
    assert user.id is not None
    await storage.set_symbol_hidden(user.id, parsed, hidden)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------- #
# Confirm flow                                                          #
# --------------------------------------------------------------------- #


@router.get("/{pending_id}/watch", response_class=HTMLResponse)
async def watch_partial(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    pending_id: UUID,
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Row-watch partial (P3 wait-for-completion).

    Polled by the result page (htmx, ~2s) until the row reaches a
    terminal state, then renders the actual ``CommandResult``. Pure
    read — the watcher NEVER executes; cli/live's approved-poll stays
    the only path from row to engine (ADR-002/ADR-013). Elapsed time
    since approval feeds the honest slow-pickup warning.
    """
    del user
    pending = await storage.get_pending_command(pending_id)
    if pending is None:
        return HTMLResponse('<div id="command-watch"><p class="muted">Command not found.</p></div>')
    elapsed: float | None = None
    if pending.confirmed_at is not None:
        elapsed = (datetime.now(UTC) - pending.confirmed_at.dt).total_seconds()
    return templates.TemplateResponse(
        request,
        "_command_watch.html",
        {
            "pending": pending,
            "elapsed_seconds": elapsed,
            "operator_tz": prefs.timezone,
            "watch_ctx": request.query_params.get("ctx", "page"),
        },
    )


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
async def confirm_submit(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    # too-many-return-statements: a state dispatcher over row states
    # (missing/already/expired/approved/rejected) x two presentation
    # modes (full page vs htmx modal) — each return IS one terminal
    # state; collapsing them would obscure the state machine.
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
        if _is_htmx(request):
            return templates.TemplateResponse(
                request,
                "_modal_result.html",
                {"pending": pending, "operator_tz": prefs.timezone, "watch_ctx": "modal"},
            )
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

    # Fleet-review #19 finding 6: nothing else gates this route on the
    # TTL — a confirm tab left open past ttl_expires_at (the daemon-side
    # expirer, cli/operator._expire_stale_pending_commands, may not have
    # swept it yet in a web-only deployment) could still approve/reject a
    # decision that arrived too late. Mirror the expirer's own transition
    # instead of acting on a stale decision.
    if pending.ttl_expires_at.dt <= now.dt:
        expired = pending.model_copy(update={"status": "expired"})
        try:
            await storage.save_pending_command(expired)
        except StorageError as exc:
            return templates.TemplateResponse(
                request,
                "command_result.html",
                {
                    "pending": pending,
                    "username": user.username,
                    "already": False,
                    "error": f"failed to persist expiry: {exc}",
                    "operator_tz": prefs.timezone,
                },
                status_code=500,
            )
        if _is_htmx(request):
            return templates.TemplateResponse(
                request,
                "_modal_result.html",
                {"pending": expired, "operator_tz": prefs.timezone, "watch_ctx": "modal"},
            )
        return templates.TemplateResponse(
            request,
            "command_result.html",
            {
                "pending": expired,
                "username": user.username,
                "already": False,
                "operator_tz": prefs.timezone,
            },
        )

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
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_modal_result.html",
            {"pending": updated, "operator_tz": prefs.timezone, "watch_ctx": "modal"},
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

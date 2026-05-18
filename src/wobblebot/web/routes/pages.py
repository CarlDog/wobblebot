"""Dashboard / stub page routes (Stage 7.1.D).

Three navigable empty stub pages prove the shell ships end-to-end:
``/dashboard``, ``/cost``, ``/audit``. Each renders the base layout
with a "Phase 7.X will fill this in" placeholder so the navigation +
auth-redirect + template rendering all exercise.

Plus a ``/`` root route that redirects to ``/dashboard`` (which in
turn auth-redirects to ``/auth/login`` if no session).

Per the Stage 7.1 design doc the stubs are deliberately content-free;
Stages 7.2-7.4 add the real cards. Operator following the nav links
proves the shell ships, validates the auth-redirect chain, and
exercises the layout chrome.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_templates

router = APIRouter(tags=["pages"])


@router.get("/")
async def root() -> Response:
    """Redirect the bare URL to the dashboard; auth-redirect kicks in
    from there if there's no session."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Dashboard placeholder — Stage 7.2 fills in cost + status cards."""
    return templates.TemplateResponse(
        request,
        "stub.html",
        {
            "page_title": "Dashboard",
            "phase_label": "Phase 7.2",
            "description": "Cost summary + open orders + session status.",
            "username": user.username,
        },
    )


@router.get("/cost", response_class=HTMLResponse)
async def cost(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Cost-ledger placeholder — Stage 7.2 fills in the LLM cost table."""
    return templates.TemplateResponse(
        request,
        "stub.html",
        {
            "page_title": "Cost",
            "phase_label": "Phase 7.2",
            "description": "Per-provider + per-role LLM cost ledger.",
            "username": user.username,
        },
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit(
    request: Request,
    user: User = Depends(require_user),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Audit-log placeholder — Stage 7.4 fills in the pending-command +
    notification + applied-suggestion audit tables."""
    return templates.TemplateResponse(
        request,
        "stub.html",
        {
            "page_title": "Audit",
            "phase_label": "Phase 7.4",
            "description": "Pending commands + notifications + applied suggestions.",
            "username": user.username,
        },
    )

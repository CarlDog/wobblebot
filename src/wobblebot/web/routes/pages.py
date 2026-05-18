"""Root + remaining-stub page routes.

The ``/`` redirect is permanent; the audit / advisor / harvester /
news stubs land in Stages 7.3 and 7.4 (this module shrinks as they
get real routes).
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

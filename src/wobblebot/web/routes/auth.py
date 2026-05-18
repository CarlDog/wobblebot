"""Auth routes — login + logout (Stage 7.1.C).

Three routes ship here:

- ``GET /auth/login`` — render the login form with a fresh CSRF
  token bound to the session.
- ``POST /auth/login`` — validate credentials (rate-limit → username
  lookup → bcrypt compare → session set → last-login bump → redirect
  to ``/dashboard``). Per ADR-017 decision 8 the rate-limit applies
  to every POST regardless of success so an attacker probing
  usernames can't trip the limit on a different IP than the operator.
- ``POST /auth/logout`` — clear the session, rotate the CSRF token,
  redirect to ``/auth/login``. CSRF-protected so a malicious page
  can't trigger a forced logout via image tag tricks.

Per ADR-016 + ADR-017 these are HTML-form routes (no JSON API);
HTMX is unnecessary for the login flow itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import verify_password
from wobblebot.web.dependencies import get_operator_storage, get_templates
from wobblebot.web.middleware import (
    LoginRateLimit,
    get_login_rate_limit,
    get_or_create_csrf_token,
    require_csrf_token,
    rotate_csrf_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """Return the request's source IP; ``"unknown"`` if Starlette can't
    determine one (synthetic test transports sometimes set ``client``
    to ``None``)."""
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Render the login form. Mints a CSRF token bound to the session."""
    # If already signed in, skip the form.
    if isinstance(request.session.get("username"), str):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    csrf = get_or_create_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf, "error": None},
    )


# pylint: disable=too-many-arguments,too-many-positional-arguments
@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    # CSRF first — invalid token shouldn't even count against the
    # rate-limit bucket (it's a probe, not a credential attempt).
    _csrf: None = Depends(require_csrf_token),
    username: str = Form(..., min_length=1, max_length=64),
    password: str = Form(..., min_length=1),
    storage: StoragePort = Depends(get_operator_storage),
    rate_limit: LoginRateLimit = Depends(get_login_rate_limit),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Validate credentials; on success set session and redirect to /dashboard."""
    ip = _client_ip(request)
    allowed = await rate_limit.allow(ip)
    if not allowed:
        # Re-render with rate-limit message; do NOT touch the credentials.
        csrf = get_or_create_csrf_token(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": csrf,
                "error": "Too many attempts. Wait a minute and try again.",
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user: User | None
    try:
        user = await storage.get_user_by_username(username)
    except StorageError:
        # Surface as a generic auth failure — never leak storage detail
        # to the login surface.
        user = None

    if user is None or not verify_password(password, user.password_hash):
        csrf = get_or_create_csrf_token(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": csrf,
                "error": "Invalid username or password.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Success — clear rate-limit, rotate CSRF (session-fixation guard),
    # set session, bump last_login.
    await rate_limit.reset(ip)
    request.session["username"] = user.username
    rotate_csrf_token(request)
    if user.id is not None:
        try:
            await storage.update_user_last_login(user.id, Timestamp(dt=datetime.now(UTC)))
        except StorageError:
            # Don't fail the login because the timestamp bookkeeping
            # failed; the session is already established.
            pass

    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@router.post("/logout")
async def logout(
    request: Request,
    _csrf: None = Depends(require_csrf_token),
) -> Response:
    """Clear the session and redirect to the login form."""
    request.session.clear()
    rotate_csrf_token(request)
    return RedirectResponse(url="/auth/login", status_code=status.HTTP_302_FOUND)

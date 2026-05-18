"""FastAPI app factory for the Phase 7 web UI.

``create_app(...)`` builds + returns the FastAPI instance the
``cli/web`` daemon serves. Per ADR-016 decision 1, the factory pattern
keeps app state explicit + per-instance — tests construct a fresh
app per test (or per fixture), production wires it once at startup.

Stage 7.1.C adds the auth flow plumbing: the ``LoginRateLimit``
singleton on ``app.state``, the :class:`AuthRedirectRequired`
exception handler that turns anonymous-access into a 302 to
``/auth/login``, and the ``csrf_input`` Jinja2 global so templates
emit the hidden form input without each template knowing how to
mint a token.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from wobblebot.config.cli import WebConfig
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import AuthRedirectRequired
from wobblebot.web.middleware import (
    CSRF_FORM_FIELD,
    LoginRateLimit,
    get_or_create_csrf_token,
)
from wobblebot.web.routes import auth as auth_routes
from wobblebot.web.routes import commands as command_routes
from wobblebot.web.routes import cost as cost_routes
from wobblebot.web.routes import pages as page_routes
from wobblebot.web.routes import status as status_routes

_WEB_PKG_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_PKG_ROOT / "templates"
_STATIC_DIR = _WEB_PKG_ROOT / "static"


def _csrf_input(request: Request) -> Markup:
    """Jinja2 global that emits the hidden CSRF input for any form.

    Templates call ``{{ csrf_input(request) }}`` inside their form
    body; this mints (or reuses) the session's token and renders the
    ``<input type="hidden">`` markup. Using a Jinja2 global instead
    of context-passing keeps every form's CSRF posture consistent —
    forget the call and the form fails CSRF on submit, surfacing the
    omission immediately.
    """
    token = get_or_create_csrf_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}">')


def create_app(
    *,
    config: WebConfig,
    operator_storage: StoragePort,
    session_secret: str,
    advise_storage: StoragePort | None = None,
    harvest_storage: StoragePort | None = None,
    observe_storage: StoragePort | None = None,
    news_storage: StoragePort | None = None,
    live_storage: StoragePort | None = None,
) -> FastAPI:
    """Build a FastAPI instance wired to the provided storage adapters.

    Args:
        config: Operator-tunable knobs (bind, session, rate-limit,
            htmx poll cadence, DB paths).
        operator_storage: REQUIRED — backs the users table + every
            mutation that crosses ``pending_commands``.
        session_secret: 32+ random bytes for signing the session
            cookie. ``cli/web`` reads this from the env var
            ``config.session_secret_env_var`` and passes through.
        advise_storage / harvest_storage / observe_storage /
            news_storage / live_storage: Optional cross-DB
            connections for the dashboards that read from them.
            ``None`` triggers the OperatorService-style graceful
            degrade (Stage 5.6.C) — cards that need the missing
            DB don't render.

    Returns:
        FastAPI app instance. Caller hands to uvicorn.
    """
    app = FastAPI(
        title="WobbleBot Dashboard",
        version="0.7.1",
        # Docs are operator-only; safe to expose since the app is
        # auth-gated. Disable in production if the operator prefers.
        docs_url="/docs",
        redoc_url=None,
    )

    # Per ADR-017 decision 1: signed session cookie via
    # Starlette's SessionMiddleware. itsdangerous-signed under the
    # hood; max_age is sliding (resets on each request).
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="wobblebot_session",
        max_age=config.session_max_age_days * 86400,
        same_site="lax",
        https_only=False,  # Set via reverse proxy's X-Forwarded-Proto in real deployments
    )

    # Static assets (HTMX + base.css).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Templates dependency wired onto app.state so route handlers
    # can pull it via the dependencies module.
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["csrf_input"] = _csrf_input
    app.state.templates = templates
    app.state.config = config
    app.state.operator_storage = operator_storage
    app.state.advise_storage = advise_storage
    app.state.harvest_storage = harvest_storage
    app.state.observe_storage = observe_storage
    app.state.news_storage = news_storage
    app.state.live_storage = live_storage

    # Login rate-limit singleton (ADR-017 decision 8). One per app
    # instance so test fixtures get isolated buckets automatically.
    app.state.login_rate_limit = LoginRateLimit(
        max_attempts=config.rate_limit_attempts,
        window_seconds=config.rate_limit_window_seconds,
    )

    @app.exception_handler(AuthRedirectRequired)
    async def _auth_redirect(_request: Request, _exc: AuthRedirectRequired) -> Response:
        """Map ``require_user`` failures to a 302 to the login page."""
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_302_FOUND)

    # Silence unused-name false positive — the decorator registers it.
    _ = _auth_redirect

    # Routers — feature areas mount their own APIRouter. Status is
    # included BEFORE pages so its /dashboard route wins over the
    # 7.1 stub.
    app.include_router(auth_routes.router)
    app.include_router(cost_routes.router)
    app.include_router(status_routes.router)
    app.include_router(command_routes.router)
    app.include_router(page_routes.router)

    return app


__all__ = ("create_app",)

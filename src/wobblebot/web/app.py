"""FastAPI app factory for the Phase 7 web UI.

``create_app(...)`` builds + returns the FastAPI instance the
``cli/web`` daemon serves. Per ADR-016 decision 1, the factory pattern
keeps app state explicit + per-instance — tests construct a fresh
app per test (or per fixture), production wires it once at startup.

Stage 7.1.B ships the factory skeleton: middleware stack, static-file
mount, Jinja2 templates wired, routers stub-included. The actual
auth flow + page routes land in Stages 7.1.C–7.1.D.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from wobblebot.config.cli import WebConfig
from wobblebot.ports.storage import StoragePort
from wobblebot.web.routes import auth as auth_routes
from wobblebot.web.routes import pages as page_routes

_WEB_PKG_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_PKG_ROOT / "templates"
_STATIC_DIR = _WEB_PKG_ROOT / "static"


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

    # Static assets (HTMX + base.css). Stage 7.1.D's templates load
    # these via /static/... URLs.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Templates dependency wired onto app.state so route handlers
    # can pull it via the dependencies module.
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.config = config
    app.state.operator_storage = operator_storage
    app.state.advise_storage = advise_storage
    app.state.harvest_storage = harvest_storage
    app.state.observe_storage = observe_storage
    app.state.news_storage = news_storage
    app.state.live_storage = live_storage

    # Routers — feature areas mount their own APIRouter. Stage 7.1.B
    # ships auth + pages skeletons; feature routers (cost, status,
    # advisor, harvester, news, audit) land in Stages 7.2–7.4.
    app.include_router(auth_routes.router)
    app.include_router(page_routes.router)

    return app

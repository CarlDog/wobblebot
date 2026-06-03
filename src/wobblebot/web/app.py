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

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import Scope

from wobblebot.config.cli import TradingMode, WebConfig
from wobblebot.ports.storage import StoragePort
from wobblebot.services.daemon_health import DaemonHealthThresholds
from wobblebot.services.kraken_health import KrakenHealthProbe
from wobblebot.web.auth import AuthRedirectRequired
from wobblebot.web.middleware import (
    CSRF_FORM_FIELD,
    LoginRateLimit,
    get_or_create_csrf_token,
)
from wobblebot.web.routes import advisor as advisor_routes
from wobblebot.web.routes import auth as auth_routes
from wobblebot.web.routes import commands as command_routes
from wobblebot.web.routes import cost as cost_routes
from wobblebot.web.routes import harvester as harvester_routes
from wobblebot.web.routes import health as health_routes
from wobblebot.web.routes import history as history_routes
from wobblebot.web.routes import news as news_routes
from wobblebot.web.routes import notifications as notifications_routes
from wobblebot.web.routes import pages as page_routes
from wobblebot.web.routes import settings as settings_routes
from wobblebot.web.routes import status as status_routes

_WEB_PKG_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_PKG_ROOT / "templates"
_STATIC_DIR = _WEB_PKG_ROOT / "static"


class _CachedStaticFiles(StaticFiles):
    """``StaticFiles`` with ``Cache-Control: public, max-age=300`` on 200s.

    Without an explicit ``Cache-Control`` header, browsers fall back
    to conditional revalidation (``If-Modified-Since`` round-trip per
    request), which adds a visible 'beat' between page navigation
    and image load for in-navbar assets like the brand mark. A
    5-minute max-age hot-caches static assets for normal browsing
    while still letting dev edits show up reasonably quickly.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["cache-control"] = "public, max-age=300"
        return response


def _tz_format(dt: datetime, tz_name: str, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Jinja filter that renders a UTC datetime in the operator's tz.

    Pure presentation conversion — the input datetime is treated as
    UTC (or whatever tz it was stored with), converted to the operator's
    preferred IANA timezone, and formatted via strftime. Underlying
    persisted values are untouched.

    Falls back to UTC silently if ``tz_name`` is not in the system's
    zoneinfo database — templates should always render something rather
    than 500. The settings POST route validates IANA names before
    persistence, so a missing-zone error here would indicate either a
    system-level zoneinfo gap or a deliberately bypassed validation.
    """
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return dt.astimezone(tz).strftime(fmt)


def _humanize_duration(seconds: float | None) -> str:
    """Jinja filter that renders a duration in seconds as a compact phrase.

    Examples: ``45s``, ``9m 23s``, ``47m``, ``11h 21m``, ``3d 7h``. Two
    significant units past the minute boundary keeps the operator's
    glance-time short — '11h 21m' beats both '40,883s' and '11.4h'.
    Returns an em-dash for ``None`` so templates can use the filter
    unconditionally without an ``{% if %}`` wrapper.
    """
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 10:
        return f"{m}m {s}s"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


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


def create_app(  # pylint: disable=too-many-arguments
    *,
    config: WebConfig,
    trading_mode: TradingMode = "live",
    operator_storage: StoragePort,
    session_secret: str,
    advise_storage: StoragePort | None = None,
    harvest_storage: StoragePort | None = None,
    observe_storage: StoragePort | None = None,
    news_storage: StoragePort | None = None,
    live_storage: StoragePort | None = None,
    kraken_health_probe: KrakenHealthProbe | None = None,
    daemon_health_thresholds: DaemonHealthThresholds | None = None,
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
        # NOTE: Swagger UI (/docs) + /openapi.json are served by FastAPI
        # itself and do NOT pass through the per-route require_user auth —
        # an unauthenticated client that can reach the app can read the
        # route topology + schema. Exposure is limited to the loopback
        # bind (127.0.0.1) behind the operator's reverse proxy (ADR-016);
        # set docs_url=None to disable entirely if you'd rather not expose it.
        docs_url="/docs",
        redoc_url=None,
    )

    # Per ADR-017 decision 1: signed session cookie via
    # Starlette's SessionMiddleware. itsdangerous-signed under the
    # hood; max_age is sliding (resets on each request).
    #
    # Cookie security flags:
    # - ``HttpOnly`` is hardcoded by Starlette's SessionMiddleware
    #   (see ``self.security_flags = "httponly; samesite=" + same_site``
    #   in starlette/middleware/sessions.py) — no parameter to set,
    #   always-on. Confirmed during the 2026-05-23 security audit.
    # - ``SameSite=lax`` set below.
    # - ``Secure`` (https_only=False here) is intentionally OFF in
    #   the app layer because cli/web binds 127.0.0.1 by default and
    #   the reverse proxy (per docs/deploy/reverse-proxy.md) is where
    #   TLS termination + the Secure flag get added. Setting
    #   https_only=True here would break local development. Operators
    #   exposing cli/web beyond loopback MUST front it with a reverse
    #   proxy that rewrites Set-Cookie to include Secure.
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="wobblebot_session",
        max_age=config.session_max_age_days * 86400,
        same_site="lax",
        https_only=False,
    )

    # Static assets (HTMX + base.css + brand mark).
    app.mount("/static", _CachedStaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Templates dependency wired onto app.state so route handlers
    # can pull it via the dependencies module.
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["csrf_input"] = _csrf_input
    # Operator-facing presentation globals — surface fields that
    # every template may need without threading them through each
    # route's context dict.
    templates.env.globals["kraken_account_url"] = config.kraken_account_url
    templates.env.globals["htmx_poll_seconds"] = config.htmx_poll_seconds
    # Footer carries the running app version. Pulled off the FastAPI
    # ``version`` attribute so a future ``pyproject.toml`` bump
    # flows through automatically.
    templates.env.globals["app_version"] = app.version
    # Deployment trading mode (live / shadow / sandbox) from the single
    # `application.mode` source — drives the dashboard mode-badge. The
    # same UI is reused across modes (no separate shadow page).
    templates.env.globals["trading_mode"] = trading_mode
    # Stage 8.4 follow-up — timezone-aware timestamp filter. Routes
    # pass the operator's tz preference (loaded from
    # user_preferences) as ``operator_tz`` in context; templates
    # render timestamps via ``{{ dt | tz_format(operator_tz) }}``.
    # **Display-only**: this filter converts UTC datetimes for
    # rendering; storage, logs, engine paths all stay UTC.
    templates.env.filters["tz_format"] = _tz_format
    # Compact duration formatter for "X ago" displays — turns
    # 40883s into "11h 21m" instead of the raw second count.
    templates.env.filters["humanize_duration"] = _humanize_duration
    app.state.templates = templates
    app.state.config = config
    app.state.operator_storage = operator_storage
    app.state.advise_storage = advise_storage
    app.state.harvest_storage = harvest_storage
    app.state.observe_storage = observe_storage
    app.state.news_storage = news_storage
    app.state.live_storage = live_storage
    # Stage 8.4.E health-icon work — KrakenHealthProbe singleton.
    # cli/web constructs one in production; tests pass None when they
    # don't care (the /health page renders Kraken as "not configured").
    app.state.kraken_health_probe = kraken_health_probe
    # Per-daemon staleness thresholds — cli/web derives from
    # WobbleBotConfig.schedules so operator-tuned cadences flow into
    # the health UI without code changes. None falls back to
    # SchedulesConfig defaults inside fetch_daemon_freshness.
    app.state.daemon_health_thresholds = daemon_health_thresholds

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
    app.include_router(advisor_routes.router)
    app.include_router(harvester_routes.router)
    app.include_router(news_routes.router)
    app.include_router(history_routes.router)
    app.include_router(health_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(notifications_routes.router)
    app.include_router(page_routes.router)

    return app


__all__ = ("create_app",)

"""Web UI middleware — CSRF synchronizer-token + login rate-limit.

Per ADR-017 decisions 7 + 8 these are *application-level* concerns
implemented as FastAPI dependencies / small helper classes rather
than Starlette ``BaseHTTPMiddleware`` subclasses — the surface area
is small enough that dependency injection keeps the wiring simple
and avoids the body-buffering footguns of middleware-level form
parsing.

Two pieces ship here:

- :class:`LoginRateLimit` — in-memory per-IP token-bucket. 5 attempts
  per 60 seconds (operator-tunable via :class:`WebConfig`). Resets on
  successful login. Single-process scope; persistent rate-limiting
  across daemon restarts would need a SQLite table and is overkill
  until Phase 8 reliability shows it's needed.
- :func:`get_or_create_csrf_token` + :func:`require_csrf_token` —
  synchronizer-token pattern. The token lives in the session cookie;
  forms include it as a hidden ``csrf_token`` input; the FastAPI
  dependency rejects POSTs whose form value doesn't match the
  session.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

# --------------------------------------------------------------------- #
# CSRF — synchronizer-token pattern                                     #
# --------------------------------------------------------------------- #


CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"


def get_or_create_csrf_token(request: Request) -> str:
    """Return the current session's CSRF token, minting one if absent.

    Called from GET handlers + the ``csrf_input`` Jinja2 global so
    every rendered form gets a token bound to the current session.
    Subsequent GETs reuse the same token; rotating per-request would
    break HTMX partial-update flows that submit forms loaded earlier.

    Token rotates on login + logout to prevent fixation: those routes
    explicitly pop ``csrf_token`` from the session before the next
    GET regenerates one.
    """
    token = request.session.get(CSRF_SESSION_KEY)
    if isinstance(token, str) and token:
        return token
    fresh = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = fresh
    return fresh


async def require_csrf_token(request: Request) -> None:
    """FastAPI dependency that rejects POSTs missing / mismatching CSRF.

    Reads ``csrf_token`` from the submitted form body, compares
    against ``session["csrf_token"]`` using ``secrets.compare_digest``
    for constant-time semantics. 403 on mismatch — same status as
    OWASP's recommendation, distinguishable from 401 (unauthenticated)
    so the operator can tell the two apart in logs.

    Only meaningful on state-changing methods (POST / PUT / PATCH /
    DELETE). GETs don't go through this — but routes that need it
    declare it via ``Depends(require_csrf_token)`` rather than the
    dependency inspecting the method.
    """
    expected = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing from session",
        )
    form = await request.form()
    submitted = form.get(CSRF_FORM_FIELD)
    if not isinstance(submitted, str) or not secrets.compare_digest(expected, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )


def rotate_csrf_token(request: Request) -> str:
    """Force a fresh CSRF token; used after login + logout transitions."""
    fresh = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = fresh
    return fresh


# --------------------------------------------------------------------- #
# Login rate-limit                                                      #
# --------------------------------------------------------------------- #


@dataclass
class _IPBucket:
    """Per-IP attempt counter inside the rolling window."""

    attempts: list[float] = field(default_factory=list)


class LoginRateLimit:
    """Token-bucket-ish login rate-limit, keyed by client IP.

    Tracks login attempts (regardless of success) within a rolling
    window keyed by ``request.client.host``. ``allow(ip)`` returns
    ``True`` if another attempt is permitted (and records it); ``False``
    if the IP has exhausted its budget for the window.

    Deployment note — under the recommended posture this is effectively a
    *global* throttle, not per-IP. The web tier is meant to sit behind a
    loopback bind + reverse proxy (ADR-016/017), and we deliberately do
    NOT parse ``X-Forwarded-For`` (``proxy_headers`` stays off, since a
    forwarded header is spoofable), so ``request.client.host`` is the
    proxy's address and every login shares one bucket. That is the
    intended behaviour for a single-operator LAN deployment — the
    limiter's job is to slow online password-guessing, and one global
    bucket does that without trusting a spoofable header. The per-IP
    keying only fractures into genuine per-client buckets if the daemon
    is ever exposed directly (no proxy) to multiple distinct clients.

    Successful login calls :meth:`reset` to clear the bucket — the
    operator should not get locked out by their own three-wrong-tries-
    then-correct pattern.

    Concurrency: ``asyncio.Lock`` guards bucket mutation. The web
    daemon is single-process but FastAPI handles each request in an
    async task; without the lock two near-simultaneous attempts could
    both see ``attempts < max`` and both succeed past the gate.

    Storage: in-memory. Daemon restart wipes the bucket — fine; the
    rate-limit's job is to slow online brute-force, not to deter an
    attacker who can restart the daemon at will (they can't from
    outside).
    """

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._buckets: dict[str, _IPBucket] = {}
        self._lock = asyncio.Lock()

    async def allow(self, ip: str) -> bool:
        """Record an attempt for ``ip``; return whether it's allowed."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            bucket = self._buckets.setdefault(ip, _IPBucket())
            # Drop attempts that aged out of the window.
            bucket.attempts = [t for t in bucket.attempts if t > cutoff]
            if len(bucket.attempts) >= self._max_attempts:
                return False
            bucket.attempts.append(now)
            return True

    async def reset(self, ip: str) -> None:
        """Clear ``ip``'s bucket — called after a successful login."""
        async with self._lock:
            self._buckets.pop(ip, None)

    async def attempts_for(self, ip: str) -> int:
        """Count of in-window attempts for ``ip`` (test introspection)."""
        cutoff = time.monotonic() - self._window_seconds
        async with self._lock:
            bucket = self._buckets.get(ip)
            if bucket is None:
                return 0
            return sum(1 for t in bucket.attempts if t > cutoff)


# --------------------------------------------------------------------- #
# FastAPI dependency to pull the singleton off app.state                #
# --------------------------------------------------------------------- #


def get_login_rate_limit(request: Request) -> LoginRateLimit:
    """Pull the singleton :class:`LoginRateLimit` off ``app.state``.

    ``create_app`` instantiates it once at startup; routes consume
    it via this dependency rather than reaching into ``app.state``
    directly so test fixtures can override via FastAPI's
    ``app.dependency_overrides``.
    """
    return request.app.state.login_rate_limit  # type: ignore[no-any-return]


__all__ = (
    "CSRF_FORM_FIELD",
    "CSRF_SESSION_KEY",
    "LoginRateLimit",
    "get_login_rate_limit",
    "get_or_create_csrf_token",
    "require_csrf_token",
    "rotate_csrf_token",
)

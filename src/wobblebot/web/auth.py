"""Web UI auth helpers — bcrypt + session lookup + auth-redirect (Stage 7.1.C).

Per ADR-017 decision 4, password hashing uses the ``bcrypt`` package
directly (no ``passlib`` abstraction). The hash string is the
``$2b$``-prefixed value bcrypt produces (~60 chars incl. salt + cost).

This module ships three concrete pieces in Stage 7.1.C:

- :func:`hash_password` + :func:`verify_password` — pure helpers.
- :func:`current_user` — FastAPI dependency reading
  ``session["username"]`` and looking up via ``StoragePort``.
  Returns ``None`` when no session or unknown username (the latter
  handles the operator-deleted-the-user-row case).
- :func:`require_user` — same lookup but raises
  :class:`AuthRedirectRequired` on anonymous; the app's exception
  handler turns that into a 302 to ``/auth/login``.

The route handlers + ``LoginRateLimit`` instance + CSRF dependency
live in :mod:`wobblebot.web.middleware` and :mod:`wobblebot.web.routes.auth`.
"""

from __future__ import annotations

import bcrypt
from fastapi import Depends, Request

from wobblebot.domain.users import User
from wobblebot.ports.storage import StoragePort
from wobblebot.web.dependencies import get_operator_storage


class AuthRedirectRequired(Exception):
    """Raised by :func:`require_user` when the session is anonymous.

    The app's exception handler (registered in ``create_app``) maps
    this to a ``302 /auth/login`` redirect so route handlers don't
    each have to check + redirect themselves.
    """


def hash_password(plaintext: str, *, cost: int = 12) -> str:
    """Hash a plaintext password with bcrypt at the given cost factor.

    Returns the ``$2b$``-prefixed hash string ready for persistence
    via ``StoragePort.create_user``. The plaintext is NEVER stored —
    only this hash output.

    Cost factor 12 is the ADR-017 default. Operator can bump via
    ``WebConfig.bcrypt_cost`` if hardware warrants.
    """
    if not plaintext:
        raise ValueError("password must be non-empty")
    salt = bcrypt.gensalt(rounds=cost)
    hashed = bcrypt.hashpw(plaintext.encode("utf-8"), salt)
    return hashed.decode("ascii")


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Constant-time bcrypt comparison.

    ``bcrypt.checkpw`` handles the constant-time semantics internally
    + tolerates the older ``$2a$`` / ``$2y$`` prefixes for legacy
    hashes (none exist in this project, but cheap to support).

    Returns ``False`` on any structural problem (empty inputs,
    malformed hash) rather than raising — auth failures should look
    indistinguishable to the caller whether the cause was a wrong
    password or a corrupt row.
    """
    if not plaintext or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash bytes / unknown prefix → treat as no-match.
        return False


async def current_user(
    request: Request,
    storage: StoragePort = Depends(get_operator_storage),
) -> User | None:
    """FastAPI dependency returning the current operator or ``None``.

    Reads ``request.session["username"]`` (set by the login route).
    Re-queries ``StoragePort.get_user_by_username`` on every request
    so a deleted account immediately invalidates outstanding sessions
    — at single-operator scope the per-request lookup is cheap and
    keeps the auth model simple.

    Returns ``None`` when no username in session OR the username
    doesn't resolve to a row (operator deleted the row mid-session).
    """
    username = request.session.get("username")
    if not isinstance(username, str) or not username:
        return None
    user = await storage.get_user_by_username(username)
    if user is None:
        # Stale session: the session cookie still names a user that
        # no longer exists in storage (operator deleted the row, OR
        # the row was renamed since this session was minted — the
        # 2026-05-23 username rename triggered exactly this redirect
        # loop). Clear the session so the next request hits
        # /auth/login with a fresh form instead of bouncing back via
        # "already signed in" → /dashboard → "stale user" → /login.
        request.session.clear()
    return user


async def require_user(
    user: User | None = Depends(current_user),
) -> User:
    """FastAPI dependency that gates a route behind a valid session.

    Routes that need an authenticated operator do
    ``user: User = Depends(require_user)`` in the signature. On
    anonymous requests this raises :class:`AuthRedirectRequired`
    which the app's exception handler turns into a 302 to
    ``/auth/login``.
    """
    if user is None:
        raise AuthRedirectRequired()
    return user

"""Auth routes — login + logout + the current-user FastAPI dependency.

Stage 7.1.B ships the empty router so ``app.py``'s
``include_router(auth.router)`` doesn't fail at import time. The
actual ``/login`` GET + POST, ``/logout`` POST, and
``current_user`` dependency land in Stage 7.1.C.
"""

from __future__ import annotations

from fastapi import APIRouter

# Mounted at /auth in app.py via include_router(prefix is set here so
# the route paths stay self-contained).
router = APIRouter(prefix="/auth", tags=["auth"])

# Stage 7.1.C will populate the router with:
# - GET  /auth/login  → render the login form
# - POST /auth/login  → validate creds, set session, redirect
# - POST /auth/logout → clear session, redirect to /auth/login

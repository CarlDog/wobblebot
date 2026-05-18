"""Web UI auth helpers (Stage 7.1.B placeholder).

Per ADR-017 decision 4, password hashing uses the ``bcrypt`` package
directly (no ``passlib`` abstraction). The actual login flow + the
``current_user`` FastAPI dependency land in Stage 7.1.C alongside the
route handlers they support.

This module ships in 7.1.B as a stable import target so the package
graph is consistent — the real implementations follow.
"""

from __future__ import annotations

# Placeholder. Stage 7.1.C will populate this module with:
# - hash_password(plaintext, cost) -> str  (bcrypt $2b$-prefixed)
# - verify_password(plaintext, hash) -> bool  (constant-time via
#   bcrypt.checkpw)
# - current_user(request, storage) -> User | None  (FastAPI dep
#   reading session["user_id"], looking up via storage)
# - require_user(...) -> User  (raises 302 to /login if anonymous)

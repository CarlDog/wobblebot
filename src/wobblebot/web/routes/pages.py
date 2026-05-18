"""Dashboard / stub page routes (Stage 7.1.B skeleton; 7.1.D fills in).

Three navigable empty stub pages prove the shell ships end-to-end:
``/dashboard``, ``/cost``, ``/audit``. Each renders the base layout
with a "Stage 7.X will fill this in" placeholder so the navigation
+ auth-redirect + template rendering all exercise.

Stage 7.1.D wires the actual stubs against ``layout.html``; this
module ships an empty router in 7.1.B as a stable import target.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["pages"])

# Stage 7.1.D will populate the router with:
# - GET / → redirect to /dashboard (or /auth/login if unauthenticated)
# - GET /dashboard → "Phase 7.2 — Cost + Status (placeholder)"
# - GET /cost      → "Phase 7.2 — Cost ledger (placeholder)"
# - GET /audit     → "Phase 7.4 — Audit log (placeholder)"

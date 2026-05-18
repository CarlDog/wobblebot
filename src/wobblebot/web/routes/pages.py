"""Root redirect route.

Once a feature is real (Stages 7.2-7.4), its route module owns the
URL — this module shrinks accordingly. After Stage 7.4 only the bare
``/`` root remains; every other surface is feature-owned.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse
from starlette.responses import Response

router = APIRouter(tags=["pages"])


@router.get("/")
async def root() -> Response:
    """Redirect the bare URL to the dashboard; auth-redirect kicks in
    from there if there's no session."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

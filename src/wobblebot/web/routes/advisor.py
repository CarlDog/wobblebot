"""Advisor view — reads advise.db's advisor_suggestions (Stage 7.3.A).

The advisor surface shows the operator what the LLM advisors have
proposed recently. Two layers per row:

1. The **aggregated recommendation** (the value Stage 3.4b's
   ``cli/apply`` would gate against if the operator chose to apply
   it).
2. The **per-expert opinions** (``AdvisorRecommendation.expert_opinions``,
   populated by ``MoEAdvisorAdapter`` per ADR-007). Visible per-row
   so the operator can audit the reasoning chain — single-LLM
   advisor output renders an empty opinions list.

Read-only; the auto-apply gate stays the only path from suggestion
to settings.yml (ADR-007 + Stage 3.4b). The view degrades gracefully
when ``advise_storage`` is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User
from wobblebot.ports.advisor import AdvisorSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_advise_storage, get_templates

router = APIRouter(tags=["advisor"])


@dataclass(frozen=True)
class AdvisorSnapshot:
    """Everything the advisor template needs in one bundle."""

    wired: bool
    suggestions: tuple[AdvisorSuggestion, ...]
    error: str | None = None


async def _load_snapshot(
    advise_storage: StoragePort | None,
) -> AdvisorSnapshot:
    """Pull recent advisor_suggestions; degrade gracefully on failure."""
    if advise_storage is None:
        return AdvisorSnapshot(wired=False, suggestions=())
    try:
        rows = await advise_storage.get_advisor_suggestions(limit=50)
    except StorageError as exc:
        return AdvisorSnapshot(
            wired=True,
            suggestions=(),
            error=f"failed to query advisor_suggestions: {exc}",
        )
    return AdvisorSnapshot(wired=True, suggestions=tuple(rows))


@router.get("/advisor", response_class=HTMLResponse)
async def advisor_page(
    request: Request,
    user: User = Depends(require_user),
    advise_storage: StoragePort | None = Depends(get_advise_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Recent advisor suggestions page."""
    snapshot = await _load_snapshot(advise_storage)
    return templates.TemplateResponse(
        request,
        "advisor.html",
        {"snapshot": snapshot, "username": user.username},
    )


__all__ = ("router", "AdvisorSnapshot")

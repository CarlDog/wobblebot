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

from wobblebot.domain.users import User, UserPreferences
from wobblebot.ports.advisor import AdvisorSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import get_advise_storage, get_templates

router = APIRouter(tags=["advisor"])

# Display the most-recent N suggestions on the page; pull a wider
# slice for the total count. Wide-slice limit set well above
# realistic soak volume (advisor cadence ~6/day × 4 weeks ~= 170);
# a future v1.1 "load more" / pagination effort would replace the
# wide-slice approach with proper COUNT() + OFFSET port methods.
_ADVISOR_DISPLAY_LIMIT = 20
_ADVISOR_QUERY_LIMIT = 1000


@dataclass(frozen=True)
class AdvisorRow:
    """A suggestion plus the display-only flags the template can't derive.

    ``below_floor`` is True when the suggestion proposes a
    ``spacing_percentage`` strictly tighter than the
    ``current_grid.spacing_percentage`` recorded in its ``input_summary``
    (what the advisor was looking at). The auto-apply floor
    (``services/auto_apply.py``, ADR-022) rejects exactly those, so they
    can never land — but the raw recommendation is kept and shown
    (de-emphasised) so per-suggestion accuracy stays trackable.
    """

    suggestion: AdvisorSuggestion
    below_floor: bool
    proposed_spacing: float | None
    current_spacing: float | None


@dataclass(frozen=True)
class AdvisorSnapshot:
    """Everything the advisor template needs in one bundle."""

    wired: bool
    rows: tuple[AdvisorRow, ...]
    total: int = 0
    error: str | None = None


def _as_float(value: object) -> float | None:
    """Coerce a JSON-ish numeric to float; None on bool / non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_row(suggestion: AdvisorSuggestion) -> AdvisorRow:
    """Wrap a suggestion with the sub-floor display flag (see AdvisorRow)."""
    proposed = _as_float(suggestion.recommendation.recommendations.get("spacing_percentage"))
    grid = suggestion.input_summary.get("current_grid")
    current = _as_float(grid.get("spacing_percentage")) if isinstance(grid, dict) else None
    below = proposed is not None and current is not None and proposed < current
    return AdvisorRow(
        suggestion=suggestion,
        below_floor=below,
        proposed_spacing=proposed,
        current_spacing=current,
    )


async def _load_snapshot(
    advise_storage: StoragePort | None,
) -> AdvisorSnapshot:
    """Pull recent advisor_suggestions; degrade gracefully on failure."""
    if advise_storage is None:
        return AdvisorSnapshot(wired=False, rows=())
    try:
        suggestions = await advise_storage.get_advisor_suggestions(limit=_ADVISOR_QUERY_LIMIT)
    except StorageError as exc:
        return AdvisorSnapshot(
            wired=True,
            rows=(),
            error=f"failed to query advisor_suggestions: {exc}",
        )
    return AdvisorSnapshot(
        wired=True,
        rows=tuple(_to_row(s) for s in suggestions[:_ADVISOR_DISPLAY_LIMIT]),
        total=len(suggestions),
    )


@router.get("/advisor", response_class=HTMLResponse)
async def advisor_page(
    request: Request,
    user: User = Depends(require_user),
    advise_storage: StoragePort | None = Depends(get_advise_storage),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Recent advisor suggestions page."""
    snapshot = await _load_snapshot(advise_storage)
    return templates.TemplateResponse(
        request,
        "advisor.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


__all__ = ("router", "AdvisorSnapshot", "AdvisorRow")

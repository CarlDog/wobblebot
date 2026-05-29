"""Per-user web UI settings (Stage 8.4 follow-up).

Two routes ship here:

- ``GET /settings`` — render the form with the operator's current
  preferences pre-filled. Loads from ``user_preferences`` (auto-
  creates the default row on first access).
- ``POST /settings`` — accept the form, validate the timezone
  against ``zoneinfo.available_timezones()``, and persist via
  ``StoragePort.update_user_preferences``. CSRF-protected.

**Display-only scope** per the Stage 8.4 audit: every preference
in this surface affects rendering on the web UI, never storage,
logs, engine paths, or any other daemon. ``Timestamp`` objects in
storage remain UTC-normalized per Stage 1's design decision; the
``tz_format`` Jinja filter (registered in ``web/app.py``) converts
UTC datetimes to the operator's preferred zone purely for display.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User, UserPreferences
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import get_operator_storage, get_templates
from wobblebot.web.middleware import require_csrf_token

router = APIRouter(tags=["settings"])

_LOGGER = logging.getLogger("wobblebot.web.routes.settings")

# Sensible default set of timezones surfaced in the dropdown. The
# operator can type any IANA string into the custom field if they
# need one not on the list. This list is a usability concession,
# not a whitelist — the form accepts any value in the system's
# zoneinfo database.
_COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Australia/Sydney",
    "Pacific/Auckland",
)


def _common_timezones_with_current(current: str) -> tuple[str, ...]:
    """Return the common-tz list with ``current`` injected (alphabetized)
    if it isn't already on the list. Keeps the dropdown showing the
    operator's actual current value even when they've set a custom one.
    """
    if current in _COMMON_TIMEZONES:
        return _COMMON_TIMEZONES
    return tuple(sorted([*_COMMON_TIMEZONES, current]))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(require_user),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Render the settings form with current preferences."""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": user.username,
            "preferences": prefs,
            "available_timezones": _common_timezones_with_current(prefs.timezone),
            "last_refreshed_at": datetime.now(UTC),
            "operator_tz": prefs.timezone,
            "save_status": request.query_params.get("save"),
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    timezone: str = Form(...),
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    _csrf: None = Depends(require_csrf_token),
) -> Response:
    """Validate + persist the operator's preferences.

    Bad timezone strings (not in zoneinfo) are rejected with a 400
    and an explanatory message in the redirect target. Successful
    saves redirect back to /settings with a `?save=ok` flag the
    template renders as a confirmation banner.
    """
    del request  # Unused; require_csrf_token already consumed the request.
    assert user.id is not None

    if timezone not in available_timezones():
        return RedirectResponse(
            url=f"/settings?save=invalid_tz&attempted={timezone}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    new_prefs = UserPreferences(
        user_id=user.id,
        timezone=timezone,
        updated_at=Timestamp(dt=datetime.now(UTC)),
    )
    try:
        await storage.update_user_preferences(new_prefs)
    except StorageError as exc:
        _LOGGER.warning(
            "failed to persist user preferences",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "user_id": user.id,
            },
        )
        return RedirectResponse(
            url="/settings?save=error",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url="/settings?save=ok",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ("router",)

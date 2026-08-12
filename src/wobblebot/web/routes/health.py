"""Application health page — Stage 8.4.E health-icon work.

* ``GET /health`` — full page split into two sections:
  **Upstream services** (Kraken's SystemStatus via the probe on
  ``app.state``) and **Daemons** (per-daemon freshness derived
  from each daemon's primary write).

The dashboard's "Trading Status" card dot is rendered inline by the
status route (stage 8.4.E follow-up 2026-05-22) — it imports
:func:`load_health_snapshot` from this module so the dot's color
travels with the same poll that refreshes the status card body. The
previous ``GET /health/icon`` HTMX fragment was removed: it polled
twice (icon every 30s, card every 15s) and the icon's
empty-then-populated swap caused a visible flicker.

Severity roll-up (operator-facing semantics):

* 🔴 **Red** — Kraken is in ``maintenance``. No trading is possible
  regardless of what local daemons say.
* 🟡 **Yellow** — anything degraded: Kraken ``cancel_only`` /
  ``post_only`` / probe-failed, or any detected daemon stale /
  unknown.
* 🟢 **Green** — Kraken online + every detected daemon fresh.

All daemons are now detected: observe / news / advise via their
frequent primary writes, and cli/live / cli/harvest / cli/operator /
cli/maintenance via the ``daemon_heartbeats`` table (Stage 8.4.E
follow-up). See :mod:`wobblebot.services.daemon_health`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse, Response

from wobblebot.config.cli import WebConfig
from wobblebot.domain.users import User, UserPreferences
from wobblebot.services.daemon_health import (
    DaemonHealth,
    DaemonHealthThresholds,
    DaemonStatus,
    fetch_daemon_freshness,
)
from wobblebot.services.kraken_health import (
    KrakenHealthProbe,
    KrakenHealthResult,
    KrakenSystemStatus,
)
from wobblebot.services.llm_call_streak import LLMCallStreak, fetch_llm_call_streaks
from wobblebot.services.llm_health import LLMEndpointHealth, LLMHealthChecker
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_config,
    get_templates,
)

router = APIRouter(tags=["health"])

_LOGGER = logging.getLogger("wobblebot.web.routes.health")

# Roles whose failure streaks are worth surfacing. `single` is what the
# cascade's escalation records itself as (verified against the live
# ledger 2026-08-11 — the 3.5-day outage is 387 `single` rows), so
# omitting it would miss the exact incident this was built for.
_STREAK_ROLES = ("single", "quant", "risk", "news", "arbitrator", "operator")


class OverallStatus(StrEnum):
    """Roll-up traffic-light state for the dashboard icon."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class HealthSnapshot:
    """Everything the ``/health`` template needs in one bundle."""

    kraken: KrakenHealthResult | None
    daemons: tuple[DaemonHealth, ...]
    overall: OverallStatus
    last_refreshed_at: datetime
    # LLM endpoint probes (P3): empty when no checker is wired (tests,
    # deployments with no LLM config) — the template omits the section.
    llm: tuple[LLMEndpointHealth, ...] = ()
    # Consecutive-failure streaks read from operator.db's llm_calls
    # (2026-08-11). The endpoint probes above hit each provider's FREE
    # models-list URL, which answers 200 on a quota-exhausted key — so
    # they read green through a 3.5-day advisor outage. These read the
    # request path that actually bills.
    llm_streaks: tuple[LLMCallStreak, ...] = ()


def compute_overall_status(
    kraken: KrakenHealthResult | None,
    daemons: tuple[DaemonHealth, ...],
    llm: tuple[LLMEndpointHealth, ...] = (),
    llm_streaks: tuple[LLMCallStreak, ...] = (),
) -> OverallStatus:
    """Roll up Kraken + per-daemon states into one traffic light.

    Red is reserved for Kraken ``maintenance`` because that's the
    only state where the operator can be certain trading is offline.
    Probe-failed is yellow — we don't know, so we don't escalate to
    red. Stale daemons are yellow regardless of which daemon, since
    none of the v1.0-detectable daemons are mission-critical for
    trading itself (cli/live runs against Kraken directly; observe /
    news / advise inform the advisor + dashboards).
    """
    if kraken is not None and kraken.status is KrakenSystemStatus.MAINTENANCE:
        return OverallStatus.RED
    has_yellow = False
    if kraken is None:
        # Probe not configured — same posture as probe_failed: we
        # can't tell.
        has_yellow = True
    elif kraken.status is not KrakenSystemStatus.ONLINE:
        # online → no contribution; everything else (cancel_only,
        # post_only, probe_failed) is yellow.
        has_yellow = True
    for d in daemons:
        if d.status is not DaemonStatus.FRESH:
            has_yellow = True
            break
    # LLM endpoints are advisory infrastructure (ADR-002) — an outage
    # degrades the advisor/operator surfaces, never trading itself, so
    # it contributes YELLOW at most.
    if any(not e.ok for e in llm):
        has_yellow = True
    # A sustained failure streak is yellow for the same reason: the
    # advisor is degraded, trading is not. `failing` is False when there
    # were no calls at all, so a cascade whose guards resolved every tick
    # does not read as broken.
    if any(s.failing for s in llm_streaks):
        has_yellow = True
    return OverallStatus.YELLOW if has_yellow else OverallStatus.GREEN


def _path_or_none(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


async def load_health_snapshot(request: Request, config: WebConfig) -> HealthSnapshot:
    """Build a :class:`HealthSnapshot` for the current request.

    Pulls the Kraken probe singleton off ``app.state`` (``None`` when
    cli/web didn't build one — tests typically) and reads daemon
    freshness off the configured DB paths. Public so the status-card
    route can compose the dashboard dot from the same data.
    """
    probe: KrakenHealthProbe | None = getattr(request.app.state, "kraken_health_probe", None)
    kraken_result = await probe.get() if probe is not None else None
    llm_checker: LLMHealthChecker | None = getattr(request.app.state, "llm_health_checker", None)
    llm = await llm_checker.get() if llm_checker is not None else ()
    thresholds: DaemonHealthThresholds | None = getattr(
        request.app.state, "daemon_health_thresholds", None
    )
    daemons = await fetch_daemon_freshness(
        observe_db=_path_or_none(config.observe_db),
        advise_db=_path_or_none(config.advise_db),
        operator_db=_path_or_none(config.operator_db),
        thresholds=thresholds,
    )
    streaks = tuple(
        await fetch_llm_call_streaks(
            operator_db=_path_or_none(config.operator_db),
            roles=_STREAK_ROLES,
        )
    )
    return HealthSnapshot(
        kraken=kraken_result,
        daemons=tuple(daemons),
        overall=compute_overall_status(kraken_result, tuple(daemons), llm, streaks),
        last_refreshed_at=datetime.now(UTC),
        llm=llm,
        llm_streaks=streaks,
    )


@router.get("/health/overall.json", response_class=JSONResponse)
async def health_overall_json(
    request: Request,
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    config: WebConfig = Depends(get_config),
) -> JSONResponse:
    """Return just the overall traffic-light status as JSON.

    Polled by ``layout.html``'s health-badge JS every 30s. Returns
    ``{"overall": "green" | "yellow" | "red"}``. Reuses the same
    ``load_health_snapshot`` builder as the full /health page so the
    nav-icon dot and the page traffic-light can never disagree.

    Cheap (Kraken probe is TTL-cached on app.state; daemon freshness
    is a single SELECT per configured DB). Failures collapse to
    ``{"overall": "green"}`` so the dot doesn't lie under transient
    storage hiccups — the /health page itself remains the source of
    truth.
    """
    try:
        snapshot = await load_health_snapshot(request, config)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Collapse to green so a transient hiccup doesn't make the nav dot
        # lie — but LOG it (this was silently swallowed): a *persistent*
        # warning here is a real, non-transient bug to investigate. The
        # /health page itself, not this cosmetic badge, is the source of truth.
        _LOGGER.warning(
            "health overall.json probe failed; reporting green: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return JSONResponse({"overall": "green"})
    return JSONResponse({"overall": snapshot.overall.value})


@router.get("/healthz", response_class=JSONResponse)
async def healthz() -> JSONResponse:
    """Unauthenticated liveness probe for the Docker HEALTHCHECK.

    Deliberately auth-free and content-free (``{"status": "ok"}``,
    nothing else): the container's healthcheck can't hold a session,
    and a liveness probe that leaks data would be worse than none.
    Everything with actual health content stays behind ``require_user``
    on ``/health``. Reachable only via the loopback port binding +
    the DSM reverse proxy, same as every other route.
    """
    return JSONResponse({"status": "ok"})


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    user: User = Depends(require_user),
    config: WebConfig = Depends(get_config),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Full application health page — Upstream + Daemons sections."""
    snapshot = await load_health_snapshot(request, config)
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


__all__ = (
    "router",
    "HealthSnapshot",
    "OverallStatus",
    "compute_overall_status",
    "load_health_snapshot",
)

"""Application health page — Stage 8.4.E health-icon work.

Two surfaces:

1. ``GET /health`` — full page split into two sections:
   **Upstream services** (Kraken's SystemStatus via the probe on
   ``app.state``) and **Daemons** (per-daemon freshness derived
   from each daemon's primary write).
2. ``GET /health/icon`` — HTML fragment containing just the
   traffic-light dot + tooltip. The dashboard's "Live trading
   status" card embeds this via HTMX polling so the icon refreshes
   without re-rendering the whole status card.

Severity roll-up (operator-facing semantics):

* 🔴 **Red** — Kraken is in ``maintenance``. No trading is possible
  regardless of what local daemons say.
* 🟡 **Yellow** — anything degraded: Kraken ``cancel_only`` /
  ``post_only`` / probe-failed, or any detected daemon stale /
  unknown.
* 🟢 **Green** — Kraken online + every detected daemon fresh.

v1.0 only detects daemons whose primary writes are frequent
(observe / news / advise). cli/live, cli/harvest, cli/operator,
cli/maintenance need a heartbeat table to detect reliably; queued
for v1.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.config.cli import WebConfig
from wobblebot.domain.users import User
from wobblebot.ports.storage import StoragePort
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
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import (
    get_config,
    get_operator_storage,
    get_templates,
)

router = APIRouter(tags=["health"])


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


def _compute_overall(
    kraken: KrakenHealthResult | None,
    daemons: tuple[DaemonHealth, ...],
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
    return OverallStatus.YELLOW if has_yellow else OverallStatus.GREEN


def _path_or_none(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


async def _load_snapshot(request: Request, config: WebConfig) -> HealthSnapshot:
    """Build a :class:`HealthSnapshot` for the current request.

    Pulls the Kraken probe singleton off ``app.state`` (``None`` when
    cli/web didn't build one — tests typically) and reads daemon
    freshness off the configured DB paths.
    """
    probe: KrakenHealthProbe | None = getattr(request.app.state, "kraken_health_probe", None)
    kraken_result = await probe.get() if probe is not None else None
    thresholds: DaemonHealthThresholds | None = getattr(
        request.app.state, "daemon_health_thresholds", None
    )
    daemons = await fetch_daemon_freshness(
        observe_db=_path_or_none(config.observe_db),
        news_db=_path_or_none(config.news_db),
        advise_db=_path_or_none(config.advise_db),
        thresholds=thresholds,
    )
    return HealthSnapshot(
        kraken=kraken_result,
        daemons=tuple(daemons),
        overall=_compute_overall(kraken_result, tuple(daemons)),
        last_refreshed_at=datetime.now(UTC),
    )


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    user: User = Depends(require_user),
    config: WebConfig = Depends(get_config),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Full application health page — Upstream + Daemons sections."""
    snapshot = await _load_snapshot(request, config)
    assert user.id is not None
    prefs = await storage.get_user_preferences(user.id)
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


@router.get("/health/icon", response_class=HTMLResponse)
async def health_icon(
    request: Request,
    user: User = Depends(require_user),  # pylint: disable=unused-argument
    config: WebConfig = Depends(get_config),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """HTMX fragment — traffic-light dot + tooltip.

    The dashboard's status card embeds this; polling refreshes only
    the dot, not the whole status panel. Hover tooltip lists every
    detected component inline so the common-case "is everything
    green?" check doesn't require a click-through.
    """
    snapshot = await _load_snapshot(request, config)
    return templates.TemplateResponse(
        request,
        "_health_icon.html",
        {"snapshot": snapshot},
    )


__all__ = (
    "router",
    "HealthSnapshot",
    "OverallStatus",
)

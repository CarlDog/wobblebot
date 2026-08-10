"""Harvester view — reads harvest.db's transfer_proposals + results (Stage 7.3.B).

The harvester surface shows the operator the treasury-transfer
state: proposals the daemon has generated + the executed-withdrawal
audit trail. Read-only.

Per ADR-003 the Harvester is the sole module with transfer authority;
this view never initiates a withdrawal. ``cli/harvest --execute``
remains the only path. Graceful-degrades when ``harvest_storage`` is
unwired.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User, UserPreferences
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import get_user_preferences, require_user
from wobblebot.web.dependencies import (
    get_harvest_storage,
    get_operator_storage,
    get_templates,
    get_withdrawal_destinations,
)

router = APIRouter(tags=["harvester"])

# How stale cli/harvest's heartbeat may be before the page warns that an
# approval will sit queued. Generous relative to the daemon's hours-long
# proposal cadence because the ADR-034 command poll beats every 15s — a
# gap this large means the process is genuinely gone, not just idle.
_HARVEST_HEARTBEAT_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class HarvesterSnapshot:
    """Everything the harvester template needs.

    ``executable_ids`` names the proposals the Execute button may be
    offered for (ADR-034): exchange→bank direction, an asset with a
    configured destination, and no prior non-failed
    ``TransferResult``. Computed here rather than in the template so
    the "is this actionable?" rule has one home — the daemon re-checks
    all of it plus four more gates before any money moves.

    ``daemon_awake`` is False when cli/harvest's heartbeat is missing or
    stale. The button still works (the row queues and waits), but the
    page says so instead of implying an approval will execute promptly.
    """

    wired: bool
    proposals: tuple[TransferProposal, ...]
    results: tuple[TransferResult, ...]
    error: str | None = None
    executable_ids: frozenset[str] = frozenset()
    daemon_awake: bool = True
    destinations: Mapping[str, str] = field(default_factory=dict)


def _executable_proposal_ids(
    proposals: Sequence[TransferProposal],
    results: Sequence[TransferResult],
    destinations: Mapping[str, str],
) -> frozenset[str]:
    """Which proposals are worth offering an Execute button for."""
    spent = {r.proposal_id for r in results if r.status != "failed"}
    return frozenset(
        p.proposal_id
        for p in proposals
        if p.direction == "exchange_to_bank"
        and p.proposal_id not in spent
        and destinations.get(p.asset)
    )


async def _harvest_daemon_awake(operator_storage: StoragePort) -> bool:
    """True when cli/harvest's heartbeat is recent enough to act.

    Fails OPEN (returns True) on a storage error: a heartbeat-read
    failure is not evidence the daemon is down, and a false "daemon
    down" banner would be worse than no banner.
    """
    try:
        beats = await operator_storage.get_daemon_heartbeats()
    except StorageError:
        return True
    last = beats.get("cli/harvest")
    if last is None:
        return False
    return (datetime.now(UTC) - last) <= _HARVEST_HEARTBEAT_GRACE


async def _load_snapshot(
    harvest_storage: StoragePort | None,
    operator_storage: StoragePort,
    destinations: Mapping[str, str],
) -> HarvesterSnapshot:
    """Pull recent transfer proposals + results."""
    if harvest_storage is None:
        return HarvesterSnapshot(wired=False, proposals=(), results=())
    try:
        proposals = await harvest_storage.get_transfer_proposals(limit=50)
        results = await harvest_storage.get_transfer_results(limit=50)
    except StorageError as exc:
        return HarvesterSnapshot(
            wired=True,
            proposals=(),
            results=(),
            error=f"failed to query harvest.db: {exc}",
        )
    return HarvesterSnapshot(
        wired=True,
        proposals=tuple(proposals),
        results=tuple(results),
        executable_ids=_executable_proposal_ids(proposals, results, destinations),
        daemon_awake=await _harvest_daemon_awake(operator_storage),
        destinations=dict(destinations),
    )


@router.get("/harvester", response_class=HTMLResponse)
async def harvester_page(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    user: User = Depends(require_user),
    harvest_storage: StoragePort | None = Depends(get_harvest_storage),
    operator_storage: StoragePort = Depends(get_operator_storage),
    destinations: dict[str, str] = Depends(get_withdrawal_destinations),
    prefs: UserPreferences = Depends(get_user_preferences),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Harvester proposals + transfer results page."""
    snapshot = await _load_snapshot(harvest_storage, operator_storage, destinations)
    return templates.TemplateResponse(
        request,
        "harvester.html",
        {
            "snapshot": snapshot,
            "username": user.username,
            "operator_tz": prefs.timezone,
        },
    )


__all__ = ("router", "HarvesterSnapshot")

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

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.users import User
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_harvest_storage, get_templates

router = APIRouter(tags=["harvester"])


@dataclass(frozen=True)
class HarvesterSnapshot:
    """Everything the harvester template needs."""

    wired: bool
    proposals: tuple[TransferProposal, ...]
    results: tuple[TransferResult, ...]
    error: str | None = None


async def _load_snapshot(
    harvest_storage: StoragePort | None,
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
    )


@router.get("/harvester", response_class=HTMLResponse)
async def harvester_page(
    request: Request,
    user: User = Depends(require_user),
    harvest_storage: StoragePort | None = Depends(get_harvest_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Harvester proposals + transfer results page."""
    snapshot = await _load_snapshot(harvest_storage)
    return templates.TemplateResponse(
        request,
        "harvester.html",
        {"snapshot": snapshot, "username": user.username},
    )


__all__ = ("router", "HarvesterSnapshot")

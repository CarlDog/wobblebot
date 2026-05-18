"""Cost dashboard — reads operator.db's llm_calls (Stage 7.2.A).

Per ADR-014 every cloud-LLM call writes a forensic ``LLMCallRecord``
row. Phase 6 wired the persistence; Phase 7.2 surfaces it to the
operator via two rollups: per-day totals (last 7 days) and
per-provider/per-role breakdown (last 24h).

Two routes ship here:

- ``GET /cost`` — full page rendered against ``cost.html``.
- ``GET /cost/card`` — fragment for HTMX-polled refresh from the
  dashboard. Same data, no layout chrome.

Both are auth-gated; no mutations.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.users import User
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_operator_storage, get_templates

router = APIRouter(tags=["cost"])


# --------------------------------------------------------------------- #
# Rollups                                                               #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class DayRollup:
    """Total cost + call count for one calendar day (UTC)."""

    day: str  # ISO YYYY-MM-DD
    cost_usd: Decimal
    call_count: int


@dataclass(frozen=True)
class GroupRollup:
    """Total cost + call count for one provider/role/model triple."""

    key: str  # display label like "anthropic / quant"
    cost_usd: Decimal
    call_count: int


@dataclass(frozen=True)
class CostSnapshot:
    """Everything the cost template needs in one immutable bundle."""

    total_24h_usd: Decimal
    total_7d_usd: Decimal
    call_count_24h: int
    call_count_7d: int
    per_day: tuple[DayRollup, ...]
    per_provider_role: tuple[GroupRollup, ...]
    error: str | None = None


def _empty_snapshot(error: str | None = None) -> CostSnapshot:
    return CostSnapshot(
        total_24h_usd=Decimal("0"),
        total_7d_usd=Decimal("0"),
        call_count_24h=0,
        call_count_7d=0,
        per_day=(),
        per_provider_role=(),
        error=error,
    )


# pylint: disable=too-many-locals
def _rollup(rows: list[LLMCallRecord], *, now: datetime) -> CostSnapshot:
    """Compute the snapshot from the last 7 days of llm_calls rows.

    Two windows: a 24h slice (for the prominent total) and the full
    7-day slice (for the per-day bars). Per-provider/role grouping
    runs on the 24h slice — short-window patterns are what an operator
    monitoring spend actually wants to see.
    """
    cutoff_24h = now - timedelta(hours=24)

    total_24h = Decimal("0")
    total_7d = Decimal("0")
    n_24h = 0
    n_7d = 0
    by_day: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))
    by_group: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))

    for row in rows:
        ts = row.timestamp.dt
        cost = row.cost_usd
        total_7d += cost
        n_7d += 1
        day_key = ts.date().isoformat()
        prev_cost, prev_count = by_day[day_key]
        by_day[day_key] = (prev_cost + cost, prev_count + 1)
        if ts >= cutoff_24h:
            total_24h += cost
            n_24h += 1
            group_key = f"{row.provider} / {row.role}"
            prev_cost_g, prev_count_g = by_group[group_key]
            by_group[group_key] = (prev_cost_g + cost, prev_count_g + 1)

    per_day = tuple(
        DayRollup(day=day, cost_usd=cost, call_count=count)
        for day, (cost, count) in sorted(by_day.items(), reverse=True)
    )
    per_group = tuple(
        sorted(
            (
                GroupRollup(key=k, cost_usd=cost, call_count=count)
                for k, (cost, count) in by_group.items()
            ),
            key=lambda g: g.cost_usd,
            reverse=True,
        )
    )
    return CostSnapshot(
        total_24h_usd=total_24h,
        total_7d_usd=total_7d,
        call_count_24h=n_24h,
        call_count_7d=n_7d,
        per_day=per_day,
        per_provider_role=per_group,
    )


async def _load_snapshot(storage: StoragePort) -> CostSnapshot:
    """Pull the last 7 days from operator.db and roll up.

    Storage failure degrades to an empty snapshot with an error
    string — the dashboard renders with a single error card rather
    than 500-ing.
    """
    now = datetime.now(UTC)
    since = Timestamp(dt=now - timedelta(days=7))
    try:
        rows = await storage.get_llm_calls(since=since)
    except StorageError as exc:
        return _empty_snapshot(error=f"failed to query llm_calls: {exc}")
    return _rollup(rows, now=now)


# --------------------------------------------------------------------- #
# Routes                                                                #
# --------------------------------------------------------------------- #


@router.get("/cost", response_class=HTMLResponse)
async def cost_page(
    request: Request,
    user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Full cost dashboard page."""
    snapshot = await _load_snapshot(storage)
    return templates.TemplateResponse(
        request,
        "cost.html",
        {
            "snapshot": snapshot,
            "username": user.username,
        },
    )


@router.get("/cost/card", response_class=HTMLResponse)
async def cost_card(
    request: Request,
    _user: User = Depends(require_user),
    storage: StoragePort = Depends(get_operator_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """HTMX fragment — just the cost card without layout chrome."""
    snapshot = await _load_snapshot(storage)
    return templates.TemplateResponse(
        request,
        "_cost_card.html",
        {"snapshot": snapshot},
    )


__all__ = (
    "router",
    "CostSnapshot",
    "DayRollup",
    "GroupRollup",
)

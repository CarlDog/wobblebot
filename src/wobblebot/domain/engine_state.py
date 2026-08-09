"""Per-symbol engine-state visibility row (ADR-030).

``cli/live`` publishes one :class:`EngineStateRow` per symbol per tick
into operator.db's ``engine_state`` table (best-effort, like the
daemon heartbeat); ``cli/web`` reads them so the dashboard can render
paused / offside truthfully instead of assuming every symbol is
active. The write is a visibility side-channel — nothing in the
trading path reads it back.

Frozen ``dataclass`` rather than the domain's usual pydantic model,
per the ADR-032 ``cost_basis.py`` precedent for pure carrier types
(ADR-030 names the shape explicitly; don't "fix" it into pydantic).
Pure domain — no config/services/adapter imports.

Naming note: distinct from ``ports.assistant.EngineStateSnapshot``,
which is the LLM prompt-input model the operator assistant consumes
(today hardcoded to ``"active"`` — the very gap this row closes for
the web tier; feeding the assistant from this table is a possible
follow-up, not part of ADR-030's scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from wobblebot.domain.value_objects import Symbol

__all__ = ("EngineStateRow",)


@dataclass(frozen=True)
class EngineStateRow:
    """One symbol's engine-visibility state at ``updated_at``.

    ``reference_price`` / ``anchored_at`` are ``None`` until the
    symbol's grid has anchored (``GridState`` doesn't exist yet) —
    an honest "no anchor" rather than a placeholder. ``offside_ticks``
    is 0 when onside; ``offside`` is derived from it at emit time so a
    paused symbol still reports its true offside state (StepResult's
    ``offside`` field is False on every non-"stepped" action and must
    not feed this row).
    """

    symbol: Symbol
    paused: bool
    offside: bool
    offside_ticks: int
    reference_price: Decimal | None
    anchored_at: datetime | None
    updated_at: datetime

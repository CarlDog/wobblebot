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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from wobblebot.domain.value_objects import Symbol

__all__ = ("EngineStateRow",)


@dataclass(frozen=True)
class EngineStateRow:  # pylint: disable=too-many-instance-attributes
    # One attribute per column of a single flat visibility row — the
    # same posture as OffsideExplanation's disable. Grouping them into
    # sub-objects would add structure the table does not have.
    """One symbol's engine-visibility state at ``updated_at``.

    ``reference_price`` / ``anchored_at`` are ``None`` until the
    symbol's grid has anchored (``GridState`` doesn't exist yet) —
    an honest "no anchor" rather than a placeholder. ``offside_ticks``
    is 0 when onside; ``offside`` is derived from it at emit time so a
    paused symbol still reports its true offside state (StepResult's
    ``offside`` field is False on every non-"stepped" action and must
    not feed this row).

    ``offside_since`` is the wall-clock start of the CURRENT offside
    episode, and is the field a duration should be rendered from.
    ``offside_ticks`` can only ever say "since cli/live last started" —
    it lives in process memory, so a deploy resets it. Captured evidence:
    at 2026-09-03T23:01:23Z cli/live logged "BTC/USD still offside at
    81190.1; parked (720 consecutive ticks)", 71 minutes into that
    daemon — so the popover read "about 1h 0m" at the configured 5.0s
    tick for a symbol parked since the 2026-08-19 anchor, understating
    by ~380x. (That line is emitted from inside ``_tick``'s ``if
    offside:`` block, which a paused symbol never reaches, so it also
    proves the count was not frozen by a pause.)

    ``offside_since`` is ``None`` in two honest cases: the symbol is
    onside, or it is offside from an episode whose start nothing
    observed (a row predating the column, or a daemon that first saw
    the symbol already outside its band). It is NEVER the boot time of
    the process that noticed — only a witnessed onside->offside
    transition writes it. That distinction is the whole feature: a
    stamp at first observation would assert a confident wrong date,
    which is the class of defect 2.0.4 was cut to remove.

    The ``starved_*`` fields (2.0.8) carry one symbol's
    ``services.grid_starvation.StarvationState``, which otherwise never
    leaves the engine's memory: ``starved_ticks`` consecutive starved
    ticks, ``starved_target`` the layout's order target,
    ``starved_refusals`` / ``starved_sells_deferred`` that retry's counts,
    and ``starved_reasons`` its refusal breakdown (reason -> count). All
    five read 0 / ``{}`` when the symbol is not starved. They exist
    because the web tier CANNOT recompute them: ``sells_deferred`` needs
    the authenticated ``TradeVolume`` maker fee (ADR-038) that cli/live
    logs and never persists.

    ``starved_ticks`` IS NOT ``offside_ticks``, and the difference is a
    trap. ``offside_ticks`` is restore-seeded at boot
    (``GridEngine.restore_offside``, called from cli/live's boot restore);
    ``GridEngine._starved`` is not — it is a plain in-memory dict built
    empty in ``__init__`` with no restore path, so ``starved_ticks``
    RESETS TO 0 ON EVERY cli/live RESTART and then climbs again from 1.
    It is a count of ticks since this process started, not the age of the
    problem, and it MUST NEVER be rendered as a duration. Multiplying it
    by the tick interval is exactly the arithmetic that made a symbol
    parked since the 2026-08-19 anchor read as "about 1h 0m" (see
    ``offside_ticks`` above): the counter was honest, the multiplication
    was the lie. There is no ``starved_since`` — nothing observes the
    transition across a restart, so there is no honest one to write.
    """

    symbol: Symbol
    paused: bool
    offside: bool
    offside_ticks: int
    reference_price: Decimal | None
    anchored_at: datetime | None
    updated_at: datetime
    offside_since: datetime | None = None
    # Defaults on all five so the existing construction sites (cli/live,
    # the adapter's reader, and the test factories) keep working
    # unchanged. This is a frozen dataclass with NO validation, so a
    # None reaching here would slide in silently and render a badge
    # built from Nones — which is why the columns behind these fields
    # are NOT NULL with defaults and the reader degrades in place
    # rather than passing a NULL through.
    #
    # CONTRACT CHANGE: starved_reasons is a Mapping, so an EngineStateRow
    # is no longer hashable — frozen=True hashes over every field, and a
    # dict in that tuple makes hash(row) raise TypeError. Nothing hashes
    # one today (rows are dict VALUES keyed by Symbol, verified by grep
    # 2026-09-05); key any new set/dict on row.symbol, not on the row.
    starved_ticks: int = 0
    starved_target: int = 0
    starved_refusals: int = 0
    starved_sells_deferred: int = 0
    starved_reasons: Mapping[str, int] = field(default_factory=dict)

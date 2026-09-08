"""Starvation-badge explanation (2.0.8).

Three unrelated states used to render identically on a symbol card as
"No open orders for this symbol.": offside-parked, STARVED, and a
healthy grid simply waiting on fills. Only the first carried a badge.
This module builds the facts the STARVED badge and its hover popover
render — how many of the layout's orders were placed, how many were
refused, how many sells were cost-basis deferred, the retry cadence,
and the full refusal breakdown under the engine's own ``binding:``
label.

Unlike :mod:`wobblebot.web.routes.status_offside` this needs **no**
second storage read: every input is already on the
:class:`~wobblebot.domain.engine_state.EngineStateRow` the badge layer
loaded. The engine has to publish them because the web tier cannot
recompute them — ``sells_deferred`` needs the authenticated
``TradeVolume`` maker fee (ADR-038) that ``cli/live`` logs and never
persists. So the builder here is a plain synchronous function, not an
``async`` loader mirroring the offside module for symmetry's sake.

**The gate lives here, not in the template.** ``build_starvation_
explanation`` returns ``None`` for a paused row, an offside row, or a
row with no starved ticks, so precedence is PAUSED > OFFSIDE >
STARVED — the same order the card's badge chain already enforces. A
template-only gate would recreate the 2026-09-04 failure exactly: the
DTO pinned on one attribute while the template gates on another, ~215
lines apart, silently disagreeing.

That the gate must cover paused and offside at all is not defensive
padding. ``GridEngine._starved`` is cleared at exactly four sites
(``resume_symbol``, re-anchor, ``placed > 0``, ``remaining_open``) —
none of them the offside or pause transition — and the whole
starvation path sits behind ``if not offside:``. A symbol that starves
and then parks keeps a FROZEN ``StarvationState`` indefinitely while
the per-tick writer keeps persisting it. Rendering it would put "0/6
placed ... retrying every 60 ticks" on a card whose badge correctly
says the engine is parked and retrying nothing: two contradictory
claims side by side, the 2026-09-03 defect class verbatim. ``cli/live``
also writes CLEARED starvation fields whenever the symbol is paused or
offside; the two layers are deliberately belt-and-braces, because a
claim that is unguarded in ANY state is a defect on a real-money
dashboard, not a polish item.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_starvation import (
    SAFETY_CAP_REASONS,
    STARVED_RETRY_EVERY_TICKS,
    describe_reasons,
)

StarvationReasonKind = Literal["cap", "non_cap", "none"]
"""Which of three exhaustive wordings the leading refusal reason earns.

``"cap"`` — the commonest reason is a real configured safety cap
(:data:`~wobblebot.services.grid_starvation.SAFETY_CAP_REASONS`), so it
names a ``settings.yml`` key the operator can go and read, and the
"other caps are evaluated after it" short-circuit clause is true of it:
``_check_safety`` returns on the FIRST failing cap.

``"non_cap"`` — the commonest reason is ``insufficient_balance`` or
``exchange_error`` (or any future reason not yet allowlisted as a cap).
Neither is a cap, neither is a ``settings.yml`` key, and both are
reached only AFTER every cap has passed — so for them the short-circuit
sentence is causally BACKWARDS and the word "cap" is simply wrong.

``"none"`` — there are no refusals at all. Routine, not an edge case: a
layout whose every level was a cost-basis-deferred SELL starves with
``refusals == 0`` and an empty breakdown. It gets neither wording, and
:attr:`StarvationExplanation.reason_breakdown` is ``None`` so a
``binding:`` line cannot render with nothing behind it.

A three-valued field rather than a boolean on purpose: two booleans can
express a combination that cannot happen, and a template branching on
``is_cap`` alone silently files the third state under the non-cap
wording — a false claim in the exact class this feature exists to stop.
"""


@dataclass(frozen=True)
class StarvationExplanation:  # pylint: disable=too-many-instance-attributes
    # One attribute per fact the sentence needs, same posture as
    # OffsideExplanation: the template formats, it never derives.
    """Why one symbol placed nothing, precomputed so Jinja only formats."""

    symbol: Symbol
    # CONSECUTIVE starved ticks, straight off the row. A COUNT, and only
    # ever a count.
    #
    # **Never render this as a duration, and never multiply it by the
    # tick interval.** Unlike ``offside_ticks``, which cli/live
    # restore-seeds at boot from the persisted row
    # (``GridEngine.restore_offside``), ``GridEngine._starved`` is a
    # plain in-memory dict with no restore path at all: this counter
    # RESETS TO 0 ON EVERY cli/live RESTART and climbs again from 1. It
    # says "ticks since this process started", never the age of the
    # problem. The asymmetry is measured, not assumed — production ETH
    # logged 31680 consecutive OFFSIDE ticks 171 seconds after container
    # start, which only a restore-seeded counter can honestly report.
    # There is deliberately no seconds field and no ``starved_since``:
    # nothing observes the transition across a restart, so there is no
    # honest duration to offer. This project has already shipped one
    # duration off by ~380x from precisely this arithmetic.
    ticks: int
    # The layout's order target — the M in "0/M placed". Always >= 1 on
    # the healthy path (``_note_layout_outcome`` clears the starved state
    # when ``target == 0``), so a zero here means a corrupt row, which
    # the builder refuses rather than renders.
    target: int
    refusals: int
    sells_deferred: int
    # The FULL breakdown from the engine's own ``describe_reasons``,
    # commonest first — "insufficient_balance x3, max_daily_spend_usd x2"
    # — not a single truncated key, so a symbol refused by two different
    # things does not silently report one. ``None`` (never the
    # "no refusals" placeholder string) when there are no refusals, so
    # the template's ``{% if %}`` is correct even if it forgets to branch
    # on :attr:`reason_kind`.
    reason_breakdown: str | None
    # The commonest refusal reason, raw — the operator asked for raw
    # keys, since a cap name IS the settings.yml key that produced it.
    # ``None`` exactly when ``reason_breakdown`` is.
    #
    # INVARIANT: ``reason_breakdown.startswith(leading_reason)``. Both
    # are ordered by ``(-count, name)``; ``reason_breakdown`` gets that
    # ordering from ``describe_reasons`` and this field re-derives it
    # locally, so the sort key is the one thing that could drift. The
    # invariant above is the assertion that pins it.
    leading_reason: str | None
    reason_kind: StarvationReasonKind

    @property
    def placed(self) -> int:
        """Always ``0`` — placing even one order is what clears starvation.

        Exposed rather than left as a literal in the template so the
        "N/M placed" phrase reads from the model on both sides.
        """
        return 0

    @property
    def retry_every_ticks(self) -> int:
        """The back-off cadence, read from the engine's own constant.

        A property, not a field: there is then no construction site that
        can pass a stale literal, and the number the card quotes is the
        number ``_starved_should_attempt`` actually uses.
        """
        return STARVED_RETRY_EVERY_TICKS


def build_starvation_explanation(row: EngineStateRow) -> StarvationExplanation | None:
    """Pure builder. ``None`` whenever the STARVED badge must not render.

    Returns ``None`` for a paused row, an offside row, a row with no
    starved ticks, or a row whose target is non-positive — see the
    module docstring for why the first two belong here and not in the
    template. On every ``None`` path the card falls back to its existing
    neutral "no open orders" text; nothing new renders.
    """
    if row.paused or row.offside or row.starved_ticks <= 0:
        return None
    if row.starved_target <= 0:
        # Starvation requires a non-empty layout; never render "0/0 placed".
        return None
    ordered = sorted(row.starved_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    leading_reason = ordered[0][0] if ordered else None
    kind: StarvationReasonKind = "none"
    if leading_reason is not None:
        kind = "cap" if leading_reason in SAFETY_CAP_REASONS else "non_cap"
    return StarvationExplanation(
        symbol=row.symbol,
        ticks=row.starved_ticks,
        target=row.starved_target,
        refusals=row.starved_refusals,
        sells_deferred=row.starved_sells_deferred,
        reason_breakdown=describe_reasons(row.starved_reasons) if ordered else None,
        leading_reason=leading_reason,
        reason_kind=kind,
    )


def build_starvation_explanations(
    engine_states: Mapping[Symbol, EngineStateRow],
) -> dict[Symbol, StarvationExplanation]:
    """One explanation per genuinely-starved row; every other row is skipped.

    Synchronous on purpose: every input is already on the row, so there
    is nothing to await. Keyed like ``engine_states``, and built only
    from rows the badge layer already judged FRESH, so a popover can
    never outlive the badge it explains.
    """
    out: dict[Symbol, StarvationExplanation] = {}
    for symbol, row in engine_states.items():
        explanation = build_starvation_explanation(row)
        if explanation is not None:
            out[symbol] = explanation
    return out


__all__ = (
    "StarvationExplanation",
    "StarvationReasonKind",
    "build_starvation_explanation",
    "build_starvation_explanations",
)

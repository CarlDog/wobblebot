"""Starvation bookkeeping for the grid engine.

A layout that places ZERO of its target orders leaves the symbol onside with
no open orders, so the engine's no-orders self-heal would re-attempt it every
tick forever. :class:`StarvationState` is the per-symbol record that drives
the back-off, and -- added 2026-09-03 -- carries WHY the layout placed nothing.

The why matters because the per-level refusal WARNING is demoted to DEBUG
while a symbol is starved. XRP/USD is the standing case: every BUY refused by
a safety cap it can never satisfy at its anchor, re-emitting the same WARNINGs
on every retry forever. Demoting them without capturing the reasons first
would trade noise for blindness, so the reason attribution here is the
REPLACEMENT for what the demotion removes, not a decorative extra.

Lives outside ``grid_engine`` for the same reason ``SellGuard`` does: the
engine is already past the file-size cap and this is a self-contained concern
with no exchange or storage access.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

# Refusal reasons that are NOT a named safety cap. All three of
# ``_try_place``'s refusal paths return the same ``"refused"`` outcome, so a
# breakdown that only knew about caps would not sum to the refusal count and
# would quietly under-report an exchange-side ordermin rejection as no reason
# at all.
REASON_INSUFFICIENT_BALANCE = "insufficient_balance"
REASON_EXCHANGE_ERROR = "exchange_error"


@dataclass(frozen=True)
class LayoutOutcome:
    """What one pass over a layout's levels actually did.

    ``reasons`` maps a refusal reason to its count and sums to ``refusals``.
    Each key is either a safety-cap reason (whatever ``_check_safety``
    returned) or one of the two module constants above.
    """

    placed: int = 0
    refusals: int = 0
    sells_deferred: int = 0
    reasons: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StarvationState:
    """A symbol's starved-state record: how long, and why.

    ``ticks`` counts CONSECUTIVE starved ticks -- not wall-clock time, and
    not retries. It advances only on ticks that reach the no-orders gate,
    which requires the symbol to be onside and unpaused, and anything that
    clears the starved state resets it, including an operator re-anchor that
    places even one order. So it reads as "consecutive starved ticks since
    the last placement or intervention", never as the age of the problem.

    The reason fields are refreshed on every retry rather than frozen at
    entry, so an hourly summary reports what is binding NOW. Conditions move
    independently: free balance can return while a cap still refuses, and a
    summary quoting an hour-old reason would send the operator after the
    wrong thing.
    """

    ticks: int
    target: int
    refusals: int
    sells_deferred: int
    reasons: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def entering(cls, outcome: LayoutOutcome, target: int) -> StarvationState:
        """The state a symbol enters on its first 0/N layout."""
        return cls(
            ticks=1,
            target=target,
            refusals=outcome.refusals,
            sells_deferred=outcome.sells_deferred,
            reasons=outcome.reasons,
        )

    def advanced(self) -> StarvationState:
        """This state one tick older, everything else unchanged."""
        return replace(self, ticks=self.ticks + 1)

    def with_outcome(self, outcome: LayoutOutcome, target: int) -> StarvationState:
        """This state's tick count, carrying a fresh retry's reasons."""
        return replace(
            self,
            target=target,
            refusals=outcome.refusals,
            sells_deferred=outcome.sells_deferred,
            reasons=outcome.reasons,
        )


def describe_reasons(reasons: Mapping[str, int]) -> str:
    """Render a refusal breakdown for an operator, commonest first.

    Deliberately says nothing about which cap is "the" blocker.
    ``_check_safety`` short-circuits on the FIRST failing cap, so a level
    refused by the per-coin inventory cap may also have been over the daily
    spend cap -- relieving the named one can leave the symbol starved on the
    next cap down. Callers word this as the first BINDING reason for that
    reason.
    """
    if not reasons:
        return "no refusals"
    ordered = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{name} x{count}" for name, count in ordered)


__all__ = (
    "REASON_EXCHANGE_ERROR",
    "REASON_INSUFFICIENT_BALANCE",
    "LayoutOutcome",
    "StarvationState",
    "describe_reasons",
)

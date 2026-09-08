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

# P3 starvation back-off (the 2026-08-09 re-anchor e2e finding): when a
# layout places ZERO orders (BUYs refused for reserved quote balance +
# SELLs cost-basis-deferred), the no-orders self-heal used to re-attempt
# the full layout EVERY tick, silently and forever. Once starved, retry
# only this often — 60 ticks ≈ 5 min at the default 5s cadence, measured
# in the writer's cadence like every other tick constant. Conditions
# that unstarve a symbol (quote balance freed, price back above cost
# basis) change on market timescales; a 5-minute probe is prompt enough
# while cutting the busy-wait ~60x. A PARTIAL layout (placed >= 1) never
# counts as starved — its standing orders make the no-orders check moot.
#
# Lives here rather than in ``grid_engine`` because it is not only the
# engine's business: anything that reports a starved symbol to an operator
# has to quote the same cadence, and a second copy would drift.
STARVED_RETRY_EVERY_TICKS = 60

# Refusal reasons that are NOT a named safety cap. All three of
# ``_try_place``'s refusal paths return the same ``"refused"`` outcome, so a
# breakdown that only knew about caps would not sum to the refusal count and
# would quietly under-report an exchange-side ordermin rejection as no reason
# at all.
REASON_INSUFFICIENT_BALANCE = "insufficient_balance"
REASON_EXCHANGE_ERROR = "exchange_error"

# The named safety caps ``GridEngine._check_safety`` can refuse on. Each one
# is also the key of the ``safety:`` setting that produced it, which is the
# whole reason a consumer may show the raw name to an operator.
REASON_MAX_ORDERS_PER_COIN = "max_orders_per_coin"
REASON_MAX_PER_COIN_EXPOSURE_USD = "max_per_coin_exposure_usd"
REASON_MAX_TOTAL_EXPOSURE_USD = "max_total_exposure_usd"
REASON_MAX_DAILY_SPEND_USD = "max_daily_spend_usd"
REASON_MAX_PER_COIN_INVENTORY_USD = "max_per_coin_inventory_usd"
REASON_MAX_TOTAL_INVENTORY_USD = "max_total_inventory_usd"

SAFETY_CAP_REASONS = frozenset(
    {
        REASON_MAX_ORDERS_PER_COIN,
        REASON_MAX_PER_COIN_EXPOSURE_USD,
        REASON_MAX_TOTAL_EXPOSURE_USD,
        REASON_MAX_DAILY_SPEND_USD,
        REASON_MAX_PER_COIN_INVENTORY_USD,
        REASON_MAX_TOTAL_INVENTORY_USD,
    }
)
"""The reason names that really are a configured safety cap.

A :class:`StarvationState`'s ``reasons`` mapping is NOT all caps. It also
carries :data:`REASON_INSUFFICIENT_BALANCE` and
:data:`REASON_EXCHANGE_ERROR`, plus ``_try_place``'s ``"safety_cap"``
fallback for a decision that somehow arrived without a named reason
(unreachable today — every ``ok=False`` return in ``GridEngine.
_check_safety`` names one of the six below). None of
those three is a ``settings.yml`` key, and insufficient balance is reached
only AFTER every cap has passed, so a consumer that calls them "the binding
cap" both misnames the state and sends the operator to grep a setting that
does not exist. Membership here is how a consumer tells the two apart.

An ALLOWLIST on purpose: a new refusal reason added to ``_try_place``
tomorrow is not a cap until someone says so here, so the honest wording is
what it gets by default. An exclusion list would call it a cap by silence.
"""


@dataclass(frozen=True)
class LayoutOutcome:
    """What one pass over a layout's levels actually did.

    ``reasons`` maps a refusal reason to its count and sums to ``refusals``.
    Each key is either a safety-cap reason (whatever ``_check_safety``
    returned — see :data:`SAFETY_CAP_REASONS`) or one of the two non-cap
    constants, :data:`REASON_INSUFFICIENT_BALANCE` /
    :data:`REASON_EXCHANGE_ERROR`.
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

    **Never render this as a duration.** Unlike ``offside_ticks``, which
    ``cli/live`` restore-seeds at boot from the persisted row
    (``GridEngine.restore_offside``), this counter is process-scoped with no
    seeding path at all: a restart drops it to zero and the symbol re-enters
    starvation on its next 0/N layout as if the problem were new. The
    asymmetry is measured, not assumed: production ETH logged ``31680
    consecutive ticks`` 171 seconds after container start — a count only a
    restore-seeded counter can honestly report, and this one is not seeded.
    Multiplying it by the tick interval therefore yields a duration that is
    right only when nothing has restarted, and this project has already
    shipped one duration off by ~380x.

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
    "REASON_MAX_DAILY_SPEND_USD",
    "REASON_MAX_ORDERS_PER_COIN",
    "REASON_MAX_PER_COIN_EXPOSURE_USD",
    "REASON_MAX_PER_COIN_INVENTORY_USD",
    "REASON_MAX_TOTAL_EXPOSURE_USD",
    "REASON_MAX_TOTAL_INVENTORY_USD",
    "SAFETY_CAP_REASONS",
    "STARVED_RETRY_EVERY_TICKS",
    "LayoutOutcome",
    "StarvationState",
    "describe_reasons",
)

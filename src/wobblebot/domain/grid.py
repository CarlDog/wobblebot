"""Pure grid math for the micro-grid engine.

No I/O. Three groups of things live here:

1. **Value objects.** ``GridLevel`` is a (side, price) pair describing
   one slot in the price ladder. ``GridSlot`` adds symbol context plus
   the order currently occupying the slot (``order_id``, possibly
   ``None`` if empty).
2. **Layout computation.** ``compute_grid_levels`` builds the initial
   ladder around a reference price.
3. **Reactive math.** ``next_counter_action`` answers "given a fill,
   where does the counter-order go?"; ``is_offside`` answers "is the
   current price outside the grid window?".

Per ADR-006 the grid is "stay parked" — these functions never re-center
or shift the layout. The engine (slice 2.2.3) handles placement, fills,
and offside reaction; this module only does deterministic arithmetic
that can be tested in microseconds with no fixtures.

Trust precondition: callers (typically wired via ``GridConfig``) supply
validated inputs (positive spacing, positive level counts). These
functions do not re-validate.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import OrderSide, Symbol, Timestamp


class GridState(BaseModel):
    """Per-symbol persistent grid anchor.

    Captures the parameters needed to reconstitute the grid layout each
    tick. Created once when the engine first sees a symbol (anchored to
    the price observed at that moment) and never re-anchored — per
    ADR-006 decision 1, the grid stays parked.

    Per ADR-006 decision 4, this is the *only* grid-related entity that
    persists. ``GridSlot`` is derived each tick from
    :func:`compute_grid_levels` plus a query of open orders.
    """

    symbol: Symbol
    reference_price: Decimal = Field(gt=Decimal("0"))
    spacing_percentage: Decimal = Field(gt=Decimal("0"))
    levels_above: int = Field(gt=0)
    levels_below: int = Field(gt=0)
    created_at: Timestamp

    class Config:
        frozen = True


class GridLevel(BaseModel):
    """One slot in the grid ladder: a side and a price.

    Used both for the initial layout returned by
    :func:`compute_grid_levels` and for counter-order targets returned
    by :func:`next_counter_action`.
    """

    side: OrderSide
    price: Decimal

    class Config:
        frozen = True


class GridSlot(BaseModel):
    """A grid level paired with the order currently occupying it.

    Per ADR-006 (decision 4): the grid is a *layout* concept and orders
    are *transient* occupants. ``order_id is None`` means the slot is
    empty and the engine should fill it on the next step (subject to
    safety caps).
    """

    symbol: Symbol
    side: OrderSide
    level_price: Decimal
    order_id: UUID | None = None

    class Config:
        frozen = True

    @property
    def is_empty(self) -> bool:
        """True when no order currently occupies this slot."""
        return self.order_id is None


def grid_spacing(reference_price: Decimal, spacing_percentage: Decimal) -> Decimal:
    """Absolute price delta between adjacent grid levels.

    ``spacing_percentage`` is interpreted as a percentage of the
    reference price (per ``config/wobblebot.example.yml``: "Grid
    spacing as percentage of base price"), so the result has units of
    the quote currency.
    """
    return reference_price * spacing_percentage / Decimal("100")


def compute_grid_levels(
    reference_price: Decimal,
    spacing_percentage: Decimal,
    levels_above: int,
    levels_below: int,
) -> list[GridLevel]:
    """Build the initial grid layout sorted by ascending price.

    BUYs sit below the reference at
    ``reference - delta``, ``reference - 2*delta``, ...,
    ``reference - levels_below * delta``. SELLs sit above at
    ``reference + delta``, ``reference + 2*delta``, ...,
    ``reference + levels_above * delta``. The reference price itself
    is *not* a grid level.

    Returned list is sorted ascending by price — the lowest BUY first,
    then BUYs marching upward, then SELLs marching upward to the
    highest. This ordering is what :func:`is_offside` and the engine's
    reconciliation logic both rely on.
    """
    delta = grid_spacing(reference_price, spacing_percentage)
    levels: list[GridLevel] = [
        GridLevel(side=OrderSide.BUY, price=reference_price - n * delta)
        for n in range(levels_below, 0, -1)
    ]
    levels.extend(
        GridLevel(side=OrderSide.SELL, price=reference_price + n * delta)
        for n in range(1, levels_above + 1)
    )
    return levels


def next_counter_action(
    filled_side: OrderSide,
    filled_price: Decimal,
    spacing: Decimal,
) -> GridLevel:
    """Counter-order target for a just-filled grid order.

    BUY at ``P`` → SELL at ``P + spacing``.
    SELL at ``P`` → BUY at ``P - spacing``.

    Pure arithmetic — does not check whether the counter price is
    still inside the grid window. The engine decides whether to act on
    the counter (it might be offside, or duplicate an existing order).
    """
    if filled_side == OrderSide.BUY:
        return GridLevel(side=OrderSide.SELL, price=filled_price + spacing)
    return GridLevel(side=OrderSide.BUY, price=filled_price - spacing)


def is_offside(price: Decimal, grid_levels: list[GridLevel]) -> bool:
    """True when ``price`` is outside the grid window (below the lowest
    BUY or above the highest SELL).

    Per ADR-006 (decision 1) the engine stays parked when offside; this
    helper exposes the boolean for the engine's offside log signal and
    the optional pause-after-N-ticks behavior.

    An empty grid is treated as offside for every price.
    """
    if not grid_levels:
        return True
    return price < grid_levels[0].price or price > grid_levels[-1].price

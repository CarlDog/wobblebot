"""Offside-badge explanation (operator note 2026-09-03).

The OFFSIDE badge on a symbol card used to carry a generic ``title``
("price outside the grid band for N ticks — engine parked") that named
neither the side, the band, nor the exit. This module builds the facts
a hover popover renders beside the badge — the badge label itself is
unchanged — in plain language: which side of the band price is on and
against which level, the anchor and spacing that define the band, how
long, and the two exits (price returns, or re-anchor).

The band is rebuilt from ``grid_state`` through
:func:`wobblebot.domain.grid.compute_grid_levels`, the same function the
engine logs its own "offside at X (band A - B)" WARNING from, so the
popover and the engine agree by construction. It is deliberately NOT
derived from open orders (the sparkline's ladder band is): the band is a
property of the ANCHOR, and an offside symbol's surviving ladder is a
partial, one-sided remnant that would misreport it. Going offside does
NOT cancel standing orders — ADR-006 parking suppresses new placement and
counters only — so an offside symbol often still has live orders, and the
popover must not claim otherwise (2026-09-03 review, finding 3).

Degrades, never breaks: no ``grid_state`` row (a symbol that never
anchored) or no current price yields a duration-only explanation; a
storage failure logs and degrades the same way. Stale engine-state rows
never reach here — the badge layer already drops them (ADR-030).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.grid import GridState, compute_grid_levels
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger(__name__)

OffsideSide = Literal["above", "below"]


@dataclass(frozen=True)
class OffsideExplanation:  # pylint: disable=too-many-instance-attributes
    # Display DTO for the popover — one attribute per fact the sentence
    # needs (same posture as ReanchorRecommendation).
    """Why one symbol is offside, precomputed so Jinja only formats."""

    symbol: Symbol
    offside_ticks: int
    # Ticks x the configured tick length. This is time SINCE cli/live LAST
    # STARTED, not since the symbol went offside: ``offside_ticks`` lives in
    # GridEngine process memory and the restore path replays only ``paused``,
    # so a restart zeroes it. The template says "since cli/live last started"
    # for exactly that reason — do not reword it into a wall-clock claim.
    # Persisting an ``offside_since`` column is the real fix, filed as a
    # follow-up (2026-09-03 review, finding 4).
    offside_seconds: float
    current_price: Decimal | None
    anchor_price: Decimal | None
    spacing_percentage: Decimal | None
    levels_above: int | None
    levels_below: int | None
    band_low: Decimal | None
    band_high: Decimal | None
    side: OffsideSide | None

    @property
    def has_band(self) -> bool:
        """True when ``grid_state`` was available and the band is known."""
        return self.band_low is not None and self.band_high is not None


def build_offside_explanation(
    row: EngineStateRow,
    grid_state: GridState | None,
    current_price: Decimal | None,
    tick_seconds: float,
) -> OffsideExplanation | None:
    """Pure builder. ``None`` when the row is not offside.

    ``side`` is set only when both the band and a current price are
    known and price is actually outside the band; a price inside the
    band with an offside row (a fresh row written a tick before price
    re-entered) yields ``None`` for side and the template falls back to
    the band-only sentence rather than asserting a direction.
    """
    if not row.offside:
        return None
    seconds = row.offside_ticks * tick_seconds
    if grid_state is None:
        return OffsideExplanation(
            symbol=row.symbol,
            offside_ticks=row.offside_ticks,
            offside_seconds=seconds,
            current_price=current_price,
            anchor_price=row.reference_price,
            spacing_percentage=None,
            levels_above=None,
            levels_below=None,
            band_low=None,
            band_high=None,
            side=None,
        )
    levels = compute_grid_levels(
        reference_price=grid_state.reference_price,
        spacing_percentage=grid_state.spacing_percentage,
        levels_above=grid_state.levels_above,
        levels_below=grid_state.levels_below,
    )
    band_low = levels[0].price if levels else None
    band_high = levels[-1].price if levels else None
    side: OffsideSide | None = None
    if current_price is not None and band_low is not None and band_high is not None:
        if current_price > band_high:
            side = "above"
        elif current_price < band_low:
            side = "below"
    return OffsideExplanation(
        symbol=row.symbol,
        offside_ticks=row.offside_ticks,
        offside_seconds=seconds,
        current_price=current_price,
        anchor_price=grid_state.reference_price,
        spacing_percentage=grid_state.spacing_percentage,
        levels_above=grid_state.levels_above,
        levels_below=grid_state.levels_below,
        band_low=band_low,
        band_high=band_high,
        side=side,
    )


async def load_offside_explanations(
    live_storage: StoragePort | None,
    engine_states: Mapping[Symbol, EngineStateRow],
    current_prices: Mapping[Symbol, Decimal],
    tick_seconds: float,
) -> dict[Symbol, OffsideExplanation]:
    """One explanation per FRESH offside row; onside rows are skipped.

    Fetches ``grid_state`` only for offside symbols (0–6 reads per
    dashboard poll). A per-symbol storage failure degrades that symbol
    to the duration-only sentence and never fails the snapshot.
    """
    out: dict[Symbol, OffsideExplanation] = {}
    for symbol, row in engine_states.items():
        if not row.offside:
            continue
        grid_state: GridState | None = None
        if live_storage is not None:
            try:
                grid_state = await live_storage.get_grid_state(symbol)
            except StorageError as exc:
                _LOGGER.warning(
                    "grid-state lookup failed for %s; offside popover degrades to duration: %s",
                    symbol,
                    exc,
                    extra={"symbol": str(symbol), "error": str(exc)},
                )
        explanation = build_offside_explanation(
            row, grid_state, current_prices.get(symbol), tick_seconds
        )
        if explanation is not None:
            out[symbol] = explanation
    return out


__all__ = ("OffsideExplanation", "build_offside_explanation", "load_offside_explanations")

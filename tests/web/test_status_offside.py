"""Offside-badge explanation builder (operator note 2026-09-03).

The popover's facts are precomputed here so Jinja only formats. The band
comes from ``grid_state`` through the engine's own ``compute_grid_levels``,
never from open orders, so an offside symbol with an empty book still
gets a full explanation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.grid import GridState
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.web.routes.status_offside import (
    build_offside_explanation,
    load_offside_explanations,
)

pytestmark = pytest.mark.unit

BTC = Symbol(base="BTC", quote="USD")


_NOW = datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC)
# BTC's real anchor date. A symbol parked since then is the case the whole
# feature exists for: the tick count said "about 2h 55m" for it.
_SINCE = datetime(2026, 8, 19, 4, 6, 58, tzinfo=UTC)


def _row(
    *, offside: bool = True, ticks: int = 40128, since: datetime | None = _SINCE
) -> EngineStateRow:
    return EngineStateRow(
        symbol=BTC,
        paused=False,
        offside=offside,
        offside_ticks=ticks,
        reference_price=Decimal("64246.4"),
        anchored_at=datetime(2026, 8, 19, 4, 6, 58, tzinfo=UTC),
        updated_at=datetime.now(UTC),
        offside_since=since,
    )


def _grid() -> GridState:
    # BTC's real 2026-09-03 anchor: 64,246.40 at 3% x 3 levels gives the
    # band 58,464.224 - 70,028.576 the engine logged all session.
    return GridState(
        symbol=BTC,
        reference_price=Decimal("64246.4"),
        spacing_percentage=Decimal("3.0"),
        levels_above=3,
        levels_below=3,
        created_at=Timestamp(dt=datetime(2026, 8, 19, 4, 6, 58, tzinfo=UTC)),
    )


class TestBuildOffsideExplanation:
    def test_above_the_band_names_the_top_level(self) -> None:
        e = build_offside_explanation(_row(), _grid(), Decimal("81174.3"), _NOW)
        assert e is not None
        assert e.side == "above"
        assert e.has_band
        assert e.band_low == Decimal("58464.224")
        assert e.band_high == Decimal("70028.576")
        assert e.anchor_price == Decimal("64246.4")
        assert (e.levels_above, e.levels_below, e.spacing_percentage) == (3, 3, Decimal("3.0"))
        # Wall clock from the persisted start, not ticks x an assumed
        # cadence: 16 days, which the tick count rendered as ~2h.
        assert e.offside_seconds == (_NOW - _SINCE).total_seconds()
        assert e.offside_seconds > 15 * 24 * 3600  # ~15.8 days

    def test_below_the_band_names_the_bottom_level(self) -> None:
        e = build_offside_explanation(_row(), _grid(), Decimal("0.199"), _NOW)
        assert e is not None and e.side == "below"

    def test_price_inside_the_band_asserts_no_direction(self) -> None:
        # A fresh offside row can precede price re-entering by one tick;
        # the popover must not claim a side it cannot see.
        e = build_offside_explanation(_row(), _grid(), Decimal("65000"), _NOW)
        assert e is not None and e.has_band and e.side is None

    def test_no_grid_state_degrades_to_duration_only(self) -> None:
        e = build_offside_explanation(_row(ticks=12), None, Decimal("81174.3"), _NOW)
        assert e is not None
        assert not e.has_band
        assert e.side is None
        # Duration survives the missing band because it comes from the row's
        # own timestamp, not from anything grid_state supplies.
        assert e.offside_seconds == (_NOW - _SINCE).total_seconds()
        assert e.anchor_price == Decimal("64246.4")  # the row's own anchor still shows

    def test_no_price_keeps_the_band_but_no_side(self) -> None:
        e = build_offside_explanation(_row(), _grid(), None, _NOW)
        assert e is not None and e.has_band and e.side is None

    def test_onside_row_yields_nothing(self) -> None:
        assert build_offside_explanation(_row(offside=False, ticks=0), _grid(), None, _NOW) is None


@pytest.mark.asyncio
class TestLoadOffsideExplanations:
    async def test_reads_grid_state_only_for_offside_rows(self) -> None:
        storage = SQLiteStorageAdapter(":memory:")
        await storage.connect()
        try:
            await storage.save_grid_state(_grid())
            sol = Symbol(base="SOL", quote="USD")
            onside = EngineStateRow(
                symbol=sol,
                paused=False,
                offside=False,
                offside_ticks=0,
                reference_price=Decimal("104.29"),
                anchored_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            out = await load_offside_explanations(
                storage, {BTC: _row(), sol: onside}, {BTC: Decimal("81174.3")}, _NOW
            )
            assert set(out) == {BTC}
            assert out[BTC].side == "above"
        finally:
            await storage.close()

    async def test_unwired_live_storage_still_explains_duration(self) -> None:
        out = await load_offside_explanations(None, {BTC: _row(ticks=12)}, {}, _NOW)
        assert out[BTC].offside_seconds == (_NOW - _SINCE).total_seconds()
        assert not out[BTC].has_band


class TestUnknownStart:
    """A NULL offside_since is the production case for BTC and ETH, whose
    episodes began before the column existed. It must render as unknown,
    never as a substituted time."""

    def test_no_seconds_when_the_start_was_never_observed(self) -> None:
        e = build_offside_explanation(_row(since=None), _grid(), Decimal("81174.3"), _NOW)
        assert e is not None
        assert e.offside_since is None
        assert e.offside_seconds is None
        # Everything else still renders: the band and side are facts about
        # price, not about when the episode started.
        assert e.has_band and e.side == "above"

    def test_the_dto_carries_no_tick_count_at_all(self) -> None:
        """It existed only to render "Parked N ticks since cli/live last
        started". Leaving it on the DTO would invite exactly that sentence
        back the next time someone wants a number on the unknown branch."""
        e = build_offside_explanation(_row(since=None, ticks=999), _grid(), None, _NOW)
        assert e is not None
        assert not hasattr(e, "offside_ticks")
        assert e.offside_seconds is None

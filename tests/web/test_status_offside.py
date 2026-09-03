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


def _row(*, offside: bool = True, ticks: int = 40128) -> EngineStateRow:
    return EngineStateRow(
        symbol=BTC,
        paused=False,
        offside=offside,
        offside_ticks=ticks,
        reference_price=Decimal("64246.4"),
        anchored_at=datetime(2026, 8, 19, 4, 6, 58, tzinfo=UTC),
        updated_at=datetime.now(UTC),
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
        e = build_offside_explanation(_row(), _grid(), Decimal("81174.3"), 5.0)
        assert e is not None
        assert e.side == "above"
        assert e.has_band
        assert e.band_low == Decimal("58464.224")
        assert e.band_high == Decimal("70028.576")
        assert e.anchor_price == Decimal("64246.4")
        assert (e.levels_above, e.levels_below, e.spacing_percentage) == (3, 3, Decimal("3.0"))
        # 40,128 ticks x 5s: the duration the badge could never say.
        assert e.offside_seconds == 40128 * 5.0

    def test_below_the_band_names_the_bottom_level(self) -> None:
        e = build_offside_explanation(_row(), _grid(), Decimal("0.199"), 5.0)
        assert e is not None and e.side == "below"

    def test_price_inside_the_band_asserts_no_direction(self) -> None:
        # A fresh offside row can precede price re-entering by one tick;
        # the popover must not claim a side it cannot see.
        e = build_offside_explanation(_row(), _grid(), Decimal("65000"), 5.0)
        assert e is not None and e.has_band and e.side is None

    def test_no_grid_state_degrades_to_duration_only(self) -> None:
        e = build_offside_explanation(_row(ticks=12), None, Decimal("81174.3"), 5.0)
        assert e is not None
        assert not e.has_band
        assert e.side is None
        assert e.offside_seconds == 60.0
        assert e.anchor_price == Decimal("64246.4")  # the row's own anchor still shows

    def test_no_price_keeps_the_band_but_no_side(self) -> None:
        e = build_offside_explanation(_row(), _grid(), None, 5.0)
        assert e is not None and e.has_band and e.side is None

    def test_onside_row_yields_nothing(self) -> None:
        assert build_offside_explanation(_row(offside=False, ticks=0), _grid(), None, 5.0) is None


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
                storage, {BTC: _row(), sol: onside}, {BTC: Decimal("81174.3")}, 5.0
            )
            assert set(out) == {BTC}
            assert out[BTC].side == "above"
        finally:
            await storage.close()

    async def test_unwired_live_storage_still_explains_duration(self) -> None:
        out = await load_offside_explanations(None, {BTC: _row(ticks=12)}, {}, 5.0)
        assert out[BTC].offside_seconds == 60.0 and not out[BTC].has_band

"""Tests for the starvation record — the reason attribution the engine's
per-level DEBUG demotion trades away.

The engine tests exercise this end to end but only ever with ONE binding
reason, because a layout usually fails the same way six times. These pin the
contracts that only show up with a mixed breakdown.
"""

from __future__ import annotations

import pytest

from wobblebot.services.grid_starvation import (
    LayoutOutcome,
    StarvationState,
    describe_reasons,
)

pytestmark = pytest.mark.unit


class TestDescribeReasons:
    def test_empty_says_so_rather_than_rendering_nothing(self) -> None:
        """An empty string here would read as a truncated log line."""
        assert describe_reasons({}) == "no refusals"

    def test_commonest_first(self) -> None:
        rendered = describe_reasons(
            {"exchange_error": 1, "max_per_coin_inventory_usd": 4, "insufficient_balance": 2}
        )
        assert rendered == (
            "max_per_coin_inventory_usd x4, insufficient_balance x2, exchange_error x1"
        )

    def test_ties_break_by_name_so_the_line_is_stable_across_ticks(self) -> None:
        """Dict order follows insertion, which follows level order — an
        operator diffing two hourly summaries should not see a reordering
        that means nothing."""
        first = describe_reasons({"b_reason": 3, "a_reason": 3})
        second = describe_reasons({"a_reason": 3, "b_reason": 3})
        assert first == second == "a_reason x3, b_reason x3"


class TestStarvationState:
    def _outcome(self, **kw: object) -> LayoutOutcome:
        base: dict[str, object] = {
            "placed": 0,
            "refusals": 6,
            "sells_deferred": 0,
            "reasons": {"cap_a": 6},
        }
        base.update(kw)
        return LayoutOutcome(**base)  # type: ignore[arg-type]

    def test_entering_starts_the_clock_at_one(self) -> None:
        state = StarvationState.entering(self._outcome(), target=6)
        assert state.ticks == 1
        assert state.reasons == {"cap_a": 6}

    def test_advancing_only_moves_the_clock(self) -> None:
        state = StarvationState.entering(self._outcome(), target=6).advanced()
        assert state.ticks == 2
        assert state.reasons == {"cap_a": 6}
        assert state.target == 6

    def test_a_refreshed_state_keeps_the_clock_and_takes_the_new_reasons(self) -> None:
        """The whole point of the refresh: an hourly summary must report what
        binds now, without pretending the symbol just started starving."""
        state = StarvationState.entering(self._outcome(), target=6).advanced().advanced()
        refreshed = state.with_outcome(
            self._outcome(refusals=3, sells_deferred=3, reasons={"cap_b": 3}), target=6
        )
        assert refreshed.ticks == 3  # not reset
        assert refreshed.reasons == {"cap_b": 3}
        assert refreshed.refusals == 3
        assert refreshed.sells_deferred == 3

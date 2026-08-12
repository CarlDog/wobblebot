"""Tests for the silent-outage guard (services/llm_call_streak).

The scenario these are written against is real: 2026-08-05 -> 08-08, the
advisor's LLM escalation returned 429s 387 times in a row and every
outward surface stayed green. ``test_reproduces_the_2026_08_05_outage``
is that incident in miniature, and it must go red.

The counterpart matters just as much: a cascade whose deterministic
guards resolved every tick makes ZERO LLM calls, which is healthy. If
"no data" ever starts reading as "failing", this guard has become the
thing it replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from wobblebot.services.llm_call_streak import (
    DEFAULT_FAILURE_THRESHOLD,
    LLMCallStreak,
    fetch_llm_call_streaks,
    streak_from_rows,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)


def _rows(*specs: tuple[float, bool, str | None]) -> list[tuple[str, int, str | None]]:
    """(hours_ago, success, error_kind) -> newest-first storage rows."""
    return [
        ((_NOW - timedelta(hours=h)).isoformat(), int(ok), kind)
        for h, ok, kind in sorted(specs, key=lambda s: s[0])
    ]


class TestStreakScan:
    def test_reproduces_the_2026_08_05_outage(self) -> None:
        """The incident this module exists for: an unbroken wall of
        exhausted retries, no success anywhere in the window."""
        streak = streak_from_rows(
            "single", _rows(*[(i * 0.5, False, "LLMRetryExhausted") for i in range(36)])
        )
        assert streak.consecutive_failures == 36
        assert streak.failing
        assert streak.has_data
        assert streak.last_error_kind == "LLMRetryExhausted"
        assert streak.last_success_at is None
        assert "36 consecutive failures" in streak.detail

    def test_no_calls_is_not_a_failure(self) -> None:
        """A cascade whose guards resolved every tick calls no LLM at
        all. Reading that as an outage is the original false signal
        pointed the other way."""
        streak = streak_from_rows("quant", [])
        assert not streak.failing
        assert not streak.has_data
        assert streak.consecutive_failures == 0
        assert "no calls" in streak.detail

    def test_streak_stops_at_the_first_success(self) -> None:
        streak = streak_from_rows(
            "single",
            _rows(
                (0.1, False, "LLMRetryExhausted"),
                (0.2, False, "LLMRetryExhausted"),
                (0.3, True, None),
                (0.4, False, "ReadError"),
            ),
        )
        assert streak.consecutive_failures == 2
        assert streak.calls_in_window == 4
        assert streak.last_success_at is not None

    def test_recovery_reads_clean_even_with_failures_behind_it(self) -> None:
        """After the operator fixes billing, the newest call succeeds and
        the page must go green immediately — not wait out the window."""
        streak = streak_from_rows(
            "single",
            _rows((0.1, True, None), *[(1.0 + i, False, "LLMRetryExhausted") for i in range(20)]),
        )
        assert streak.consecutive_failures == 0
        assert not streak.failing
        assert "most recent succeeded" in streak.detail

    def test_two_failures_is_below_the_threshold(self) -> None:
        """The retry layer already absorbs blips, but two exhausted calls
        is still short of a pattern worth waking someone for."""
        streak = streak_from_rows(
            "single", _rows((0.1, False, "ReadError"), (0.2, False, "ReadError"))
        )
        assert streak.consecutive_failures == 2
        assert not streak.failing
        assert DEFAULT_FAILURE_THRESHOLD == 3

    def test_ordering_is_load_bearing(self) -> None:
        """The scan trusts newest-first. Fed oldest-first, the same rows
        describe a different world — pinned so the SQL ORDER BY can't be
        dropped without a red test."""
        newest_first = _rows((0.1, False, "X"), (0.2, False, "X"), (0.3, True, None))
        assert streak_from_rows("single", newest_first).consecutive_failures == 2
        assert streak_from_rows("single", list(reversed(newest_first))).consecutive_failures == 0


class TestFetch:
    async def _make_db(self, path: Path, rows: list[tuple[str, str, int, str | None]]) -> None:
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                "CREATE TABLE llm_calls (timestamp TEXT, role TEXT, success INT, error_kind TEXT)"
            )
            await conn.executemany(
                "INSERT INTO llm_calls (timestamp, role, success, error_kind) VALUES (?,?,?,?)",
                rows,
            )
            await conn.commit()

    @pytest.mark.asyncio
    async def test_reads_per_role_and_respects_the_window(self, tmp_path: Path) -> None:
        db = tmp_path / "operator.db"
        old = (_NOW - timedelta(hours=48)).isoformat()
        await self._make_db(
            db,
            [
                ((_NOW - timedelta(hours=1)).isoformat(), "single", 0, "LLMRetryExhausted"),
                ((_NOW - timedelta(hours=2)).isoformat(), "single", 0, "LLMRetryExhausted"),
                ((_NOW - timedelta(hours=3)).isoformat(), "single", 0, "LLMRetryExhausted"),
                ((_NOW - timedelta(hours=1)).isoformat(), "operator", 1, None),
                (old, "single", 1, None),  # outside the window — must not end the streak
            ],
        )
        streaks = {
            s.role: s
            for s in await fetch_llm_call_streaks(
                operator_db=db, roles=("single", "operator"), now=_NOW
            )
        }
        assert streaks["single"].failing
        assert streaks["single"].consecutive_failures == 3
        assert streaks["single"].last_success_at is None
        assert not streaks["operator"].failing
        assert streaks["operator"].has_data

    @pytest.mark.asyncio
    async def test_unreadable_db_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        """This feeds a health page. An unreadable ledger must not take
        the page down — and must NOT read as healthy either."""
        streaks = await fetch_llm_call_streaks(
            operator_db=tmp_path / "does-not-exist.db", roles=("single",), now=_NOW
        )
        assert len(streaks) == 1
        assert streaks[0].unavailable_reason is not None
        assert not streaks[0].failing
        assert not streaks[0].has_data
        assert "unavailable" in streaks[0].detail

    @pytest.mark.asyncio
    async def test_unconfigured_db_is_reported_not_silently_green(self, tmp_path: Path) -> None:
        del tmp_path
        streaks = await fetch_llm_call_streaks(operator_db=None, roles=("single",))
        assert streaks[0].unavailable_reason == "operator_db not configured"


class TestOverallRollup:
    def test_a_failing_streak_turns_the_light_yellow(self) -> None:
        from wobblebot.web.routes.health import OverallStatus, compute_overall_status

        failing = LLMCallStreak(
            role="single",
            consecutive_failures=387,
            calls_in_window=387,
            last_error_kind="LLMRetryExhausted",
            last_success_at=None,
            window_hours=24.0,
            threshold=3,
        )
        assert compute_overall_status(None, (), (), (failing,)) is OverallStatus.YELLOW

    def test_a_quiet_cascade_does_not_turn_it_yellow(self) -> None:
        """Zero calls must not be indistinguishable from total failure —
        that inversion is the whole bug."""
        from wobblebot.web.routes.health import compute_overall_status

        quiet = streak_from_rows("single", [])
        # Kraken is None here, which is independently yellow; the point is
        # that the streak itself contributes nothing.
        assert not quiet.failing
        assert compute_overall_status(None, (), (), (quiet,)) == compute_overall_status(
            None, (), ()
        )

"""Tests for services.cool_down (ADR-024 session-loss-cap cool-down gate)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.cool_down import check_cool_down

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 6, 5, 5, 0, 0, tzinfo=UTC)


class TestCheckCoolDown:
    def test_no_prior_trip_never_in_cool_down(self) -> None:
        status = check_cool_down(None, now=_NOW, window_minutes=60.0)
        assert status.active is False
        assert status.resumes_at is None

    def test_none_window_disables_gate_regardless_of_trip_history(self) -> None:
        recent_trip = Timestamp(dt=_NOW - timedelta(minutes=1))
        status = check_cool_down(recent_trip, now=_NOW, window_minutes=None)
        assert status.active is False

    def test_within_window_is_active(self) -> None:
        trip = Timestamp(dt=_NOW - timedelta(minutes=30))
        status = check_cool_down(trip, now=_NOW, window_minutes=60.0)
        assert status.active is True
        assert status.resumes_at == trip.dt + timedelta(minutes=60)

    def test_past_window_is_not_active(self) -> None:
        trip = Timestamp(dt=_NOW - timedelta(minutes=90))
        status = check_cool_down(trip, now=_NOW, window_minutes=60.0)
        assert status.active is False
        assert status.resumes_at is None

    def test_exactly_at_boundary_is_not_active(self) -> None:
        """now == resumes_at clears -- >=, not strictly >."""
        trip = Timestamp(dt=_NOW - timedelta(minutes=60))
        status = check_cool_down(trip, now=_NOW, window_minutes=60.0)
        assert status.active is False

    def test_one_second_before_boundary_is_active(self) -> None:
        trip = Timestamp(dt=_NOW - timedelta(minutes=60) + timedelta(seconds=1))
        status = check_cool_down(trip, now=_NOW, window_minutes=60.0)
        assert status.active is True

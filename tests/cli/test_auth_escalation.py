"""ADR-037 — auth-escalation state machines (cli/live + shared halt)."""

from __future__ import annotations

import pytest

from wobblebot.cli._common import PermanentAuthHalt
from wobblebot.cli.live import (
    _DMS_FAILURE_STREAK_ALERT,
    _LOCKOUT_BACKOFF_INITIAL_SECONDS,
    _LOCKOUT_BACKOFF_MAX_SECONDS,
    _PERMANENT_AUTH_STRIKES,
    _AuthEscalation,
)
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit


def _invalid_key() -> ExchangeError:
    return ExchangeError("boom", codes=["EAPI:Invalid key"])


def _lockout() -> ExchangeError:
    return ExchangeError("boom", codes=["EGeneral:Temporary lockout"])


class TestLockoutBackoff:
    def test_first_lockout_opens_initial_window(self) -> None:
        esc = _AuthEscalation()
        assert not esc.in_backoff()
        assert esc.note_lockout() == _LOCKOUT_BACKOFF_INITIAL_SECONDS
        assert esc.in_backoff()

    def test_windows_double_to_cap(self) -> None:
        esc = _AuthEscalation()
        seen = [esc.note_lockout() for _ in range(10)]
        assert seen[0] == _LOCKOUT_BACKOFF_INITIAL_SECONDS
        assert seen[1] == _LOCKOUT_BACKOFF_INITIAL_SECONDS * 2
        assert max(seen) == _LOCKOUT_BACKOFF_MAX_SECONDS
        assert seen[-1] == _LOCKOUT_BACKOFF_MAX_SECONDS

    def test_success_clears_backoff(self) -> None:
        esc = _AuthEscalation()
        esc.note_lockout()
        esc.note_success()
        assert not esc.in_backoff()
        # Next lockout starts a FRESH window, not a doubled one.
        assert esc.note_lockout() == _LOCKOUT_BACKOFF_INITIAL_SECONDS


class TestPermanentAuthStrikes:
    def test_third_strike_trips_exactly_once(self) -> None:
        esc = _AuthEscalation()
        results = [esc.note_permanent_auth() for _ in range(_PERMANENT_AUTH_STRIKES + 2)]
        assert results.count(True) == 1
        assert results[_PERMANENT_AUTH_STRIKES - 1] is True
        assert esc.auth_paused

    def test_success_resets_strikes(self) -> None:
        esc = _AuthEscalation()
        esc.note_permanent_auth()
        esc.note_permanent_auth()
        esc.note_success()
        assert not esc.note_permanent_auth()
        assert not esc.auth_paused


class TestDmsFailureStreak:
    def test_alert_fires_exactly_at_threshold(self) -> None:
        esc = _AuthEscalation()
        results = [esc.note_dms_failure() for _ in range(_DMS_FAILURE_STREAK_ALERT + 3)]
        assert results.count(True) == 1
        assert results[_DMS_FAILURE_STREAK_ALERT - 1] is True

    def test_success_reports_recovery_after_episode(self) -> None:
        esc = _AuthEscalation()
        for _ in range(_DMS_FAILURE_STREAK_ALERT):
            esc.note_dms_failure()
        assert esc.note_success() is True  # episode ended — emit recovered
        assert esc.note_success() is False  # steady state — quiet

    def test_short_streak_recovers_quietly(self) -> None:
        esc = _AuthEscalation()
        esc.note_dms_failure()
        assert esc.note_success() is False


class TestPermanentAuthHalt:
    def test_three_permanent_failures_halt_once(self) -> None:
        halt = PermanentAuthHalt("observe.balance_poll")
        results = [halt.note_failure(_invalid_key()) for _ in range(5)]
        assert results.count(True) == 1
        assert results[2] is True
        assert halt.halted

    def test_transient_failure_breaks_the_chain(self) -> None:
        """Only an UNBROKEN run of credential errors proves the key is
        dead — a lockout between strikes resets the count."""
        halt = PermanentAuthHalt("observe.balance_poll")
        assert not halt.note_failure(_invalid_key())
        assert not halt.note_failure(_invalid_key())
        assert not halt.note_failure(_lockout())
        assert not halt.note_failure(_invalid_key())
        assert not halt.note_failure(_invalid_key())
        assert halt.note_failure(_invalid_key())

    def test_success_resets(self) -> None:
        halt = PermanentAuthHalt("harvest.balance_read")
        halt.note_failure(_invalid_key())
        halt.note_failure(_invalid_key())
        halt.note_success()
        assert not halt.note_failure(_invalid_key())
        assert not halt.halted

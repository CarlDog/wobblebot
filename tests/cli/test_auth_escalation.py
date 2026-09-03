"""ADR-037 — auth-escalation state machines (cli/live + shared halt)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

    def test_dms_success_reports_recovery_after_episode(self) -> None:
        esc = _AuthEscalation()
        for _ in range(_DMS_FAILURE_STREAK_ALERT):
            esc.note_dms_failure()
        assert esc.note_dms_success() is True  # episode ended — emit recovered
        assert esc.note_dms_success() is False  # steady state — quiet

    def test_short_streak_recovers_quietly(self) -> None:
        esc = _AuthEscalation()
        esc.note_dms_failure()
        assert esc.note_dms_success() is False

    def test_unrelated_private_call_success_does_not_touch_dms_streak(self) -> None:
        """2026-08-20 incident regression: during a real Kraken outage,
        CancelAllOrdersAfter (the DMS reset) failed ~40 times back to
        back while OpenOrders (a DIFFERENT private endpoint, polled every
        tick via ``note_success``) kept succeeding. The pre-fix code
        shared one counter between them, so every OpenOrders success
        wiped the DMS-failure streak back to 0 before it could ever
        reach the alert threshold — the "DMS resets failing" critical
        never fired despite the sustained outage. This reproduces that
        exact interleaving and asserts the alert now fires anyway."""
        esc = _AuthEscalation()
        fired = []
        for _ in range(40):
            fired.append(esc.note_dms_failure())
            # A DIFFERENT, unrelated private call (e.g. get_open_orders)
            # keeps succeeding this whole episode -- endpoint-specific
            # degradation, not an account-wide auth failure.
            esc.note_success()
        assert fired.count(True) == 1
        assert esc.dms_failure_streak == 40  # never reset by the unrelated successes
        # Only a genuine DMS reset success ends the episode.
        assert esc.note_dms_success() is True
        assert esc.dms_failure_streak == 0

    def test_generic_success_only_recovers_lockout_or_permanent_auth(self) -> None:
        esc = _AuthEscalation()
        esc.note_lockout()
        assert esc.note_success() is True  # ended a lockout episode
        assert esc.note_success() is False  # steady state — quiet


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


class TestDmsDeadlineNote:
    """2026-09-03 follow-up: the first failure of a DMS streak names
    Kraken's last CONFIRMED auto-cancel deadline at WARNING, so a
    post-mortem can compare it against the moment the book vanished."""

    def test_no_confirmed_deadline_says_so(self) -> None:
        esc = _AuthEscalation()
        assert esc.dms_deadline_note(datetime.now(UTC)) == (
            "no confirmed auto-cancel deadline this session"
        )

    def test_future_deadline_reports_seconds_remaining(self) -> None:
        esc = _AuthEscalation()
        now = datetime(2026, 9, 3, 7, 1, 3, tzinfo=UTC)
        esc.dms_trigger_at = now + timedelta(seconds=113)
        assert esc.dms_deadline_note(now) == (
            "last confirmed auto-cancel deadline 07:02:56Z (113s from now)"
        )

    def test_past_deadline_reports_seconds_elapsed(self) -> None:
        esc = _AuthEscalation()
        now = datetime(2026, 9, 3, 7, 3, 14, tzinfo=UTC)
        esc.dms_trigger_at = now - timedelta(seconds=18)
        assert esc.dms_deadline_note(now) == (
            "last confirmed auto-cancel deadline 07:02:56Z (passed 18s ago)"
        )


class TestDmsDegradedFraction:
    """2026-09-03 follow-up: the confirmed-deadline check alone missed the real
    purge, which landed ~18s BEFORE the client-side deadline. An elapsed-window
    predicate catches it."""

    def test_healthy_switch_reports_zero(self) -> None:
        esc = _AuthEscalation()
        assert esc.dms_degraded_fraction(datetime.now(UTC), 120) == 0.0

    def test_disabled_switch_reports_zero_even_mid_streak(self) -> None:
        esc = _AuthEscalation()
        esc.dms_streak_started_at = datetime(2026, 9, 3, 7, 1, 13, tzinfo=UTC)
        assert esc.dms_degraded_fraction(datetime(2026, 9, 3, 7, 2, 38, tzinfo=UTC), None) == 0.0

    def test_the_real_incident_lands_above_the_threshold(self) -> None:
        from wobblebot.cli.live import _DMS_CALM_FRAMING_FRACTION

        esc = _AuthEscalation()
        # Streak began 07:01:13; the book vanished 07:02:38. 85s of a 120s window.
        esc.dms_streak_started_at = datetime(2026, 9, 3, 7, 1, 13, tzinfo=UTC)
        frac = esc.dms_degraded_fraction(datetime(2026, 9, 3, 7, 2, 38, tzinfo=UTC), 120)
        assert 0.70 < frac < 0.72
        assert frac >= _DMS_CALM_FRAMING_FRACTION, "the 2026-09-03 purge must frame as calm"

    def test_a_brief_blip_stays_below_the_threshold(self) -> None:
        from wobblebot.cli.live import _DMS_CALM_FRAMING_FRACTION

        esc = _AuthEscalation()
        esc.dms_streak_started_at = datetime(2026, 9, 3, 7, 1, 13, tzinfo=UTC)
        frac = esc.dms_degraded_fraction(datetime(2026, 9, 3, 7, 1, 28, tzinfo=UTC), 120)
        assert frac < _DMS_CALM_FRAMING_FRACTION, "15s of a 120s window is not a purge"

    def test_the_streak_clock_starts_once_and_clears_on_recovery(self) -> None:
        esc = _AuthEscalation()
        esc.note_dms_failure()
        first = esc.dms_streak_started_at
        assert first is not None
        esc.note_dms_failure()
        assert esc.dms_streak_started_at == first, "the clock must not restart mid-streak"
        esc.note_dms_success()
        assert esc.dms_streak_started_at is None

"""Tests for the cli/operator stale-heartbeat push alerts (P3 slice 1).

The 2026-07-20 NAS reboot left the restart:"no" money-path daemons
(cli/live + cli/harvest) dead for 11 days while the pull-only /health
page had the data and nothing pushed it. These tests pin the tracker's
transition/debounce rules — most importantly that a daemon ALREADY
stale on the very first check alerts immediately (the reboot scenario:
cli/operator came back, the money-path daemons didn't).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from wobblebot.cli import operator as operator_cli
from wobblebot.cli.operator import _heartbeat_alert_loop, _HeartbeatAlertTracker
from wobblebot.ports.notifier import Notification
from wobblebot.services.daemon_health import (
    DaemonHealth,
    DaemonHealthThresholds,
    DaemonStatus,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _health(
    name: str = "cli/live",
    *,
    status: DaemonStatus = DaemonStatus.STALE,
    age_seconds: float = 3600.0,
    threshold_seconds: float = 315.0,
) -> DaemonHealth:
    return DaemonHealth(
        name=name,
        label=name.removeprefix("cli/").title(),
        status=status,
        last_seen=None if status is DaemonStatus.UNKNOWN else _NOW - timedelta(seconds=age_seconds),
        threshold_seconds=threshold_seconds,
    )


class TestTrackerTransitions:
    def test_stale_on_first_check_alerts_immediately(self) -> None:
        """THE reboot scenario: state resets with the operator daemon,
        so anything already down when it comes back must alert on the
        first check — not wait for a fresh->stale transition that
        already happened while nobody was watching."""
        tracker = _HeartbeatAlertTracker()
        alerts = tracker.evaluate([_health("cli/live")], _NOW)
        assert len(alerts) == 1
        level, title, _message, context = alerts[0]
        assert level == "critical"
        assert "gone stale" in title
        assert context["daemon"] == "cli/live"

    def test_fresh_to_stale_transition_alerts(self) -> None:
        tracker = _HeartbeatAlertTracker()
        assert tracker.evaluate([_health(status=DaemonStatus.FRESH)], _NOW) == []
        alerts = tracker.evaluate([_health()], _NOW + timedelta(minutes=1))
        assert len(alerts) == 1

    def test_no_realert_within_repeat_window(self) -> None:
        tracker = _HeartbeatAlertTracker(repeat_seconds=3600.0)
        assert len(tracker.evaluate([_health()], _NOW)) == 1
        assert tracker.evaluate([_health()], _NOW + timedelta(minutes=30)) == []

    def test_repeat_alert_after_window(self) -> None:
        tracker = _HeartbeatAlertTracker(repeat_seconds=3600.0)
        assert len(tracker.evaluate([_health()], _NOW)) == 1
        later = _NOW + timedelta(hours=2)
        alerts = tracker.evaluate([_health()], later)
        assert len(alerts) == 1
        assert "still stale" in alerts[0][1]

    def test_recovery_emits_info_once_and_clears(self) -> None:
        tracker = _HeartbeatAlertTracker()
        tracker.evaluate([_health()], _NOW)
        recovered = tracker.evaluate(
            [_health(status=DaemonStatus.FRESH)], _NOW + timedelta(minutes=5)
        )
        assert len(recovered) == 1
        assert recovered[0][0] == "info"
        assert "recovered" in recovered[0][1]
        # Fresh again: nothing more.
        assert tracker.evaluate([_health(status=DaemonStatus.FRESH)], _NOW) == []

    def test_unknown_never_alerts_and_preserves_stale_memory(self) -> None:
        """UNKNOWN is no-signal, not failure: a blipped DB read mid-outage
        must neither alert, count as recovery, nor re-trigger the
        transition alert when the STALE reading returns."""
        tracker = _HeartbeatAlertTracker(repeat_seconds=3600.0)
        assert len(tracker.evaluate([_health()], _NOW)) == 1
        blip = _NOW + timedelta(minutes=1)
        assert tracker.evaluate([_health(status=DaemonStatus.UNKNOWN)], blip) == []
        back = _NOW + timedelta(minutes=2)
        assert tracker.evaluate([_health()], back) == []  # still inside repeat window

    def test_money_path_severity_split(self) -> None:
        tracker = _HeartbeatAlertTracker()
        alerts = tracker.evaluate([_health("cli/harvest"), _health("cli/news")], _NOW)
        by_daemon = {a[3]["daemon"]: a[0] for a in alerts}
        assert by_daemon == {"cli/harvest": "critical", "cli/news": "warning"}

    def test_operator_self_row_skipped(self) -> None:
        tracker = _HeartbeatAlertTracker()
        assert tracker.evaluate([_health("cli/operator")], _NOW) == []

    def test_muted_daemon_never_alerts_or_recovers(self) -> None:
        """P3 slice 2: operator.heartbeat_alert_mute — a deliberately-down
        daemon (harvest, per the 2026-08-08 operator decision) emits no
        alerts, no repeats, and no recovery notices; unmuted daemons in
        the same evaluation still alert."""
        tracker = _HeartbeatAlertTracker(muted=frozenset({"cli/harvest"}))
        alerts = tracker.evaluate([_health("cli/harvest"), _health("cli/live")], _NOW)
        assert [a[3]["daemon"] for a in alerts] == ["cli/live"]
        # Repeat window elapsed: still nothing for the muted daemon.
        later = _NOW + timedelta(days=1)
        alerts = tracker.evaluate([_health("cli/harvest"), _health("cli/live")], later)
        assert [a[3]["daemon"] for a in alerts] == ["cli/live"]
        # Muted daemon comes back: no recovery notice either.
        assert (
            tracker.evaluate(
                [_health("cli/harvest", status=DaemonStatus.FRESH)],
                later + timedelta(minutes=1),
            )
            == []
        )

    def test_alert_message_carries_age_and_threshold(self) -> None:
        tracker = _HeartbeatAlertTracker()
        alerts = tracker.evaluate([_health(age_seconds=11 * 86400)], _NOW)
        _level, _title, message, _context = alerts[0]
        assert "11.0d" in message
        assert "threshold" in message


class _RecordingNotifier:
    """Minimal NotifierPort stand-in: records every Notification."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send_notification(self, notification: Notification) -> None:
        self.sent.append(notification)


@pytest.mark.asyncio
class TestAlertLoop:
    async def test_loop_emits_notification_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_fetch(**_kwargs: object) -> list[DaemonHealth]:
            return [_health("cli/live")]

        monkeypatch.setattr(operator_cli, "fetch_daemon_freshness", _fake_fetch)
        notifier = _RecordingNotifier()
        stop = asyncio.Event()
        task = asyncio.create_task(
            _heartbeat_alert_loop(
                notifier=notifier,
                observe_db=None,
                advise_db=None,
                operator_db=None,
                thresholds=DaemonHealthThresholds(),
                stop_event=stop,
                check_seconds=3600.0,  # one cycle, then sleep until stopped
            )
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert len(notifier.sent) == 1
        assert notifier.sent[0].level == "critical"
        assert "cli/live" in notifier.sent[0].message

    async def test_loop_survives_freshness_read_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(**_kwargs: object) -> list[DaemonHealth]:
            raise OSError("db file vanished")

        monkeypatch.setattr(operator_cli, "fetch_daemon_freshness", _boom)
        notifier = _RecordingNotifier()
        stop = asyncio.Event()
        task = asyncio.create_task(
            _heartbeat_alert_loop(
                notifier=notifier,
                observe_db=None,
                advise_db=None,
                operator_db=None,
                thresholds=DaemonHealthThresholds(),
                stop_event=stop,
                check_seconds=3600.0,
            )
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)  # must not raise
        assert notifier.sent == []

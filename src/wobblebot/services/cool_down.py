"""Session-loss-cap cool-down gate (ADR-024).

Pure decision logic, split out for testability per the ADR — the
storage read + CLI wiring live in ``cli/live.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from wobblebot.domain.value_objects import Timestamp


@dataclass(frozen=True)
class CoolDownStatus:
    """Whether a new session may start right now.

    ``resumes_at`` is ``None`` when not in cool-down (or the gate is
    disabled) and otherwise the timestamp the window lifts.
    """

    active: bool
    resumes_at: datetime | None = None


def check_cool_down(
    last_trip_at: Timestamp | None,
    *,
    now: datetime,
    window_minutes: float | None,
) -> CoolDownStatus:
    """Decide whether a loss-cap cool-down is currently in effect.

    ``last_trip_at`` is ``None`` when no trip has ever been recorded —
    never in cool-down. ``window_minutes`` of ``None`` disables the
    gate entirely regardless of trip history (operator opt-out).
    """
    if last_trip_at is None or window_minutes is None:
        return CoolDownStatus(active=False)
    resumes_at = last_trip_at.dt + timedelta(minutes=window_minutes)
    if now >= resumes_at:
        return CoolDownStatus(active=False)
    return CoolDownStatus(active=True, resumes_at=resumes_at)


__all__ = ["CoolDownStatus", "check_cool_down"]

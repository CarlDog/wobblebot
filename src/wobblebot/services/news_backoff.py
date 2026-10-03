"""Bounded, per-source retry cadence for sustained news transport failures.

State belongs to one daemon session. A restart tries every source again; this
never edits operator configuration or permanently disables a source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SourceBackoff:
    """Keep the first retry at normal cadence, then back off up to six hours."""

    interval_seconds: float
    failures: int = 0
    next_attempt: float = 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_attempt

    def failed(self, now: float) -> float:
        """Record a failure and return the bounded delay, without sleeping."""
        self.failures += 1
        delay = min(
            self.interval_seconds * 2 ** min(self.failures - 1, 16),
            max(self.interval_seconds, 6 * 60 * 60),
        )
        self.next_attempt = now + delay
        return float(delay)

    def succeeded(self) -> None:
        """An empty but successful fetch also restores normal polling."""
        self.failures = 0
        self.next_attempt = 0.0

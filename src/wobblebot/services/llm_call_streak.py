"""Consecutive-failure detection for LLM calls (the silent-outage guard).

**Why this exists.** On 2026-08-05 the advisor's LLM escalation started
returning 429s and did not stop for 3.5 days — 387 consecutive failures,
zero successes. Nothing reported it. ``CascadingAdvisorAdapter`` caught
each failure and fell back to the heuristic verdict, which with no guard
firing is HOLD, so the daemon kept writing rows at exactly its normal
36/day with plausible rationale text and a legitimate
``role='heuristic'``. A held advisor and a dead advisor look identical
from outside. The same signature appears seven weeks earlier in the o3
era: 981 attempts on 2026-06-18, 975 exhausted-retry failures, 6
successes, no alarm. Both were found only by reading the ledger months
later.

**Why the existing probe cannot catch it.** ``services/llm_health``
probes each provider's models-list endpoint (``GET /v1/models``) — free
and non-billable by design. A quota-exhausted key still answers 200
there, so ``/health`` read "OpenAI: OK" throughout both outages. That is
a reachability proxy; this module reads the request path that actually
matters.

**Design notes.**

- **Report the streak, not a boolean.** "12 consecutive failures, last
  success 3h ago, last error LLMRetryExhausted" is actionable;
  "unhealthy" is not.
- **No calls is NOT a failure.** A cascade whose guards resolved every
  tick legitimately makes zero LLM calls, and so does a stopped daemon.
  Collapsing "no data" into "all failing" would reintroduce exactly the
  false signal this exists to kill, pointed the other way — so
  :attr:`LLMCallStreak.has_data` is separate from
  :attr:`LLMCallStreak.failing`.
- **Per role**, because an operator-assistant outage and a quant
  escalation outage are different problems with different urgency.

Direct read-only ``aiosqlite`` against the DB path, mirroring
``services/daemon_health`` — the health surfaces are observability
tooling and deliberately do not hold a writable storage adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

# Three in a row is past coincidence: the retry layer already absorbs
# transient blips, so each row here is an EXHAUSTED call, not one bad
# HTTP response.
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_WINDOW_HOURS = 24.0


@dataclass(frozen=True)
class LLMCallStreak:  # pylint: disable=too-many-instance-attributes
    """How the most recent LLM calls for one role have been going.

    Eight fields trips pylint's 7-attribute default. This is a frozen
    DTO, not a class with behaviour — every field is a distinct fact the
    /health row needs, and grouping ``window_hours`` + ``threshold``
    behind a policy object would thread an extra type through four
    functions and every test to satisfy a metric rather than a reader.
    """

    role: str
    consecutive_failures: int
    calls_in_window: int
    last_error_kind: str | None
    last_success_at: datetime | None
    window_hours: float
    threshold: int
    #: Set when the DB could not be read at all (missing file, locked,
    #: no ``llm_calls`` table). Distinct from "no calls" — we don't know.
    unavailable_reason: str | None = None

    @property
    def has_data(self) -> bool:
        """Were there any calls to judge? Zero calls is not a failure."""
        return self.calls_in_window > 0

    @property
    def failing(self) -> bool:
        """Enough consecutive failures to be worth an operator's attention."""
        return self.consecutive_failures >= self.threshold

    @property
    def detail(self) -> str:
        """One line for the /health row — the streak, stated plainly."""
        if self.unavailable_reason is not None:
            return f"unavailable ({self.unavailable_reason})"
        if not self.has_data:
            return f"no calls in the last {self.window_hours:g}h"
        if self.consecutive_failures == 0:
            return f"{_plural(self.calls_in_window, 'call')}, most recent succeeded"
        parts = [f"{_plural(self.consecutive_failures, 'consecutive failure')}"]
        if self.last_error_kind:
            parts.append(f"last error {self.last_error_kind}")
        if self.last_success_at is None:
            parts.append(f"no success in the last {self.window_hours:g}h")
        else:
            age = datetime.now(UTC) - self.last_success_at
            parts.append(f"last success {age.total_seconds() / 3600:.1f}h ago")
        return "; ".join(parts)


def _plural(count: int, noun: str) -> str:
    """``1 call`` / ``12 calls`` — the /health row is operator-facing."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def streak_from_rows(
    role: str,
    rows: Sequence[tuple[str, int, str | None]],
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> LLMCallStreak:
    """Build a streak from ``(timestamp_iso, success, error_kind)`` rows.

    ``rows`` must be ordered NEWEST FIRST — the streak is a scan from the
    head until the first success, so the ordering is load-bearing rather
    than cosmetic.

    Pure; the I/O lives in :func:`fetch_llm_call_streaks`.
    """
    consecutive = 0
    last_error: str | None = None
    last_success: datetime | None = None
    for timestamp_iso, success, error_kind in rows:
        if success:
            if last_success is None:
                last_success = _parse(timestamp_iso)
            break
        consecutive += 1
        if last_error is None:
            last_error = error_kind
    return LLMCallStreak(
        role=role,
        consecutive_failures=consecutive,
        calls_in_window=len(rows),
        last_error_kind=last_error,
        last_success_at=last_success,
        window_hours=window_hours,
        threshold=threshold,
    )


def _parse(value: str) -> datetime | None:
    """ISO-8601 from the storage layer; naive values are assumed UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def fetch_llm_call_streaks(
    *,
    operator_db: Path | None,
    roles: Sequence[str],
    window_hours: float = DEFAULT_WINDOW_HOURS,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
    now: datetime | None = None,
) -> list[LLMCallStreak]:
    """Read the recent call outcomes per role from ``operator.db``.

    The LLM cost ledger lives in operator.db regardless of which daemon
    made the call — verified against the live deployment 2026-08-11,
    where advise.db's own ``llm_calls`` table is empty and all 1598 rows
    sit in operator.db.

    A DB that can't be read yields ``unavailable_reason`` rather than
    raising: this feeds a health page, and an unreadable ledger must not
    take the page down.
    """
    if operator_db is None:
        return [
            _unavailable(role, "operator_db not configured", window_hours, threshold)
            for role in roles
        ]
    cutoff = ((now or datetime.now(UTC)) - timedelta(hours=window_hours)).isoformat()
    try:
        uri = f"file:{operator_db}?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as conn:
            out = []
            for role in roles:
                async with conn.execute(
                    "SELECT timestamp, success, error_kind FROM llm_calls "
                    "WHERE role = ? AND timestamp >= ? ORDER BY timestamp DESC",
                    (role, cutoff),
                ) as cursor:
                    rows = await cursor.fetchall()
                out.append(
                    streak_from_rows(
                        role,
                        [(str(r[0]), int(r[1]), r[2]) for r in rows],
                        window_hours=window_hours,
                        threshold=threshold,
                    )
                )
            return out
    except (aiosqlite.Error, OSError) as exc:
        return [_unavailable(role, type(exc).__name__, window_hours, threshold) for role in roles]


def _unavailable(role: str, reason: str, window_hours: float, threshold: int) -> LLMCallStreak:
    return LLMCallStreak(
        role=role,
        consecutive_failures=0,
        calls_in_window=0,
        last_error_kind=None,
        last_success_at=None,
        window_hours=window_hours,
        threshold=threshold,
        unavailable_reason=reason,
    )


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_WINDOW_HOURS",
    "LLMCallStreak",
    "fetch_llm_call_streaks",
    "streak_from_rows",
]

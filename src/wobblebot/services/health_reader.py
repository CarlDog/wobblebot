"""Startup-bound, read-only database capabilities for HTTP health observations.

These grants constrain application path selection, not a hostile filesystem.
Path checks and SQLite opens are not atomic; mount/OS permissions remain required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG

from wobblebot.services.daemon_health import (
    DaemonHealth,
    DaemonHealthThresholds,
    fetch_daemon_freshness,
)
from wobblebot.services.llm_call_streak import LLMCallStreak, fetch_llm_call_streaks

# Cascade escalation records itself as "single"; retain it alongside panel roles.
_STREAK_ROLES = ("single", "quant", "risk", "news", "arbitrator", "operator")


@dataclass
class _DatabaseGrant:
    """One canonical startup target; replacements require reader reconstruction."""

    path: Path | None
    identity: tuple[int, int] | None = None

    @classmethod
    def capture(cls, raw: str | None) -> _DatabaseGrant:
        try:
            grant = cls(Path(raw).resolve() if raw else None)
        except (OSError, RuntimeError, ValueError):
            return cls(None)
        grant.allowed_path()
        return grant

    def allowed_path(self) -> Path | None:
        """Bind a missing-at-startup file on first appearance, then pin identity."""
        if self.path is None:
            return None
        try:
            # Catch redirected canonical names/parents. Original configured aliases
            # are deliberately never followed again after startup resolution.
            if self.path.resolve() != self.path:
                return None
            info = self.path.stat(follow_symlinks=False)
            if not S_ISREG(info.st_mode):
                return None
            current = (info.st_dev, info.st_ino)
            if self.identity is None:
                self.identity = current
            return self.path if current == self.identity else None
        except (OSError, RuntimeError, ValueError):
            return None


@dataclass(frozen=True)
class HealthDatabaseReader:
    """Narrow capability: callers may observe health but cannot select files.

    Construct at startup. No request/config argument is accepted by ``read``.
    Grants pin existing file identities (or the first regular file that appears).
    A detected replacement invalidates that observation until startup rebinds it.
    """

    _observe: _DatabaseGrant = field(repr=False)
    _advise: _DatabaseGrant = field(repr=False)
    _operator: _DatabaseGrant = field(repr=False)
    _thresholds: DaemonHealthThresholds | None = field(repr=False)

    @classmethod
    def bind(
        cls,
        *,
        observe_db: str | None,
        advise_db: str | None,
        operator_db: str | None,
        thresholds: DaemonHealthThresholds | None = None,
    ) -> HealthDatabaseReader:
        """Capture operator-selected files before serving HTTP requests."""
        return cls(
            _DatabaseGrant.capture(observe_db),
            _DatabaseGrant.capture(advise_db),
            _DatabaseGrant.capture(operator_db),
            thresholds,
        )

    async def read(self) -> tuple[tuple[DaemonHealth, ...], tuple[LLMCallStreak, ...]]:
        """Read only granted files; discard results if identity changed mid-read."""
        grants = (self._observe, self._advise, self._operator)
        paths = tuple(grant.allowed_path() for grant in grants)
        try:
            daemons = await fetch_daemon_freshness(
                observe_db=paths[0],
                advise_db=paths[1],
                operator_db=paths[2],
                thresholds=self._thresholds,
            )
            streaks = await fetch_llm_call_streaks(operator_db=paths[2], roles=_STREAK_ROLES)
        except OSError:
            return await self._unavailable()
        if any(grant.allowed_path() != path for grant, path in zip(grants, paths, strict=True)):
            # Do not publish observations gathered across a detected file swap.
            return await self._unavailable()
        return tuple(daemons), tuple(streaks)

    async def _unavailable(self) -> tuple[tuple[DaemonHealth, ...], tuple[LLMCallStreak, ...]]:
        daemons = await fetch_daemon_freshness(
            observe_db=None, advise_db=None, operator_db=None, thresholds=self._thresholds
        )
        streaks = await fetch_llm_call_streaks(operator_db=None, roles=_STREAK_ROLES)
        return tuple(daemons), tuple(streaks)

"""Unified scheduling configuration for all long-running daemons.

Every periodic process in the application reads its cadence from
``settings.yml -> schedules`` rather than carrying its own
``*_interval_seconds`` / ``*_cadence_hours`` field. The operator
sees every schedule in one place; the CLIs look up by name.

Duration strings use a single unit suffix: ``s`` seconds, ``m``
minutes, ``h`` hours, ``d`` days. ``0`` (or any unit-prefixed zero
like ``0s``) is reserved for "disabled" semantics where the
consumer supports it (e.g. observe's balance polling).

Example::

    schedules:
      observe_prices: 30s
      observe_balances: 10m
      news: 30m
      advise: 4h

``SchedulesConfig`` is a ``RootModel`` over ``dict[str, timedelta]``
so the YAML mapping flows straight into a typed object with named
accessors. Adding a new schedule means adding a key in YAML and a
lookup in the consuming CLI; no per-CLI interval fields proliferate.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from pydantic import RootModel, field_validator

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_TO_SECONDS: dict[str, float] = {
    "": 1.0,  # bare number means seconds
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Parse a duration string / number into a ``timedelta``.

    Accepts:
    - ``"30s"``, ``"10m"``, ``"4h"``, ``"7d"`` — unit-suffixed.
    - ``"45"`` (string of digits) — interpreted as seconds.
    - ``30`` or ``30.5`` (number) — interpreted as seconds.
    - ``timedelta`` — returned as-is.

    Negative durations raise ``ValueError``. Zero is allowed (use it
    for "disabled" semantics where the consumer supports it).
    """
    if isinstance(value, timedelta):
        if value < timedelta(0):
            raise ValueError(f"duration must be non-negative; got {value}")
        return value
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"duration must be non-negative; got {value}")
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        raise ValueError(
            f"duration must be a string, number, or timedelta; got {type(value).__name__}"
        )
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(
            f"could not parse duration {value!r}; "
            "expected forms like '30s', '10m', '4h', '7d', or a bare number of seconds"
        )
    quantity = float(match.group(1))
    unit = match.group(2).lower()
    seconds = quantity * _UNIT_TO_SECONDS[unit]
    return timedelta(seconds=seconds)


class SchedulesConfig(RootModel[dict[str, timedelta]]):
    """All periodic-task cadences keyed by task name.

    The YAML maps task name → duration string; this model parses each
    duration into a ``timedelta``. Look up by name via ``get`` /
    ``get_or_default``.
    """

    @field_validator("root", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> dict[str, timedelta]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("schedules must be a mapping")
        return {str(k): parse_duration(v) for k, v in value.items()}

    def get(self, name: str) -> timedelta:
        """Return the cadence for a named schedule.

        Raises:
            KeyError: If the schedule isn't configured. Hint nudges the
                operator toward the fix.
        """
        try:
            return self.root[name]
        except KeyError as exc:
            raise KeyError(
                f"schedule {name!r} not configured; " "add it under `schedules:` in settings.yml"
            ) from exc

    def get_or_default(self, name: str, default: timedelta) -> timedelta:
        """Return the cadence for ``name``, falling back to ``default`` if absent."""
        return self.root.get(name, default)

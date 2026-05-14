"""Logging configuration for WobbleBot.

One function: ``configure_logging``. Called once from the CLI entry
point (or test fixtures). Idempotent — repeated calls replace the
handler instead of stacking them.

Two output formats:

- ``plain`` (default in dev): one-line human-readable text.
- ``json``: one JSON object per record, suitable for log aggregators
  in a Phase 2+ container deployment.

Both write to ``stderr`` by default. Level and format can be set
via env vars ``WOBBLEBOT_LOG_LEVEL`` and ``WOBBLEBOT_LOG_FORMAT``,
overridden by explicit arguments.

Module loggers use ``logging.getLogger(__name__)``, so a record from
``wobblebot.adapters.sqlite_storage`` carries that full dotted name.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import IO, Literal

LogFormat = Literal["plain", "json"]

_HANDLER_NAME = "wobblebot-root"
_DEFAULT_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Picks up arbitrary fields from ``logger.info("...", extra={"k": "v"})``
    by diffing the record's ``__dict__`` against the stock LogRecord
    attributes set.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # Surface any caller-supplied extras (e.g. order_id, symbol).
        for key, value in record.__dict__.items():
            if key in _DEFAULT_RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, default=str)


_PLAIN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(
    level: str | None = None,
    log_format: LogFormat | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Configure the root logger for the wobblebot package.

    Args:
        level: Logging level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Defaults to ``$WOBBLEBOT_LOG_LEVEL`` or ``INFO``.
        log_format: ``"plain"`` (human-readable) or ``"json"`` (one
            object per line). Defaults to ``$WOBBLEBOT_LOG_FORMAT`` or
            ``"plain"``.
        stream: Output stream. Defaults to ``sys.stderr``.

    Idempotent. Calling twice replaces the existing handler rather
    than stacking a second one.
    """
    resolved_level = (level or os.environ.get("WOBBLEBOT_LOG_LEVEL") or "INFO").upper()
    resolved_format: LogFormat = (
        log_format or os.environ.get("WOBBLEBOT_LOG_FORMAT") or "plain"  # type: ignore[assignment]
    )
    if resolved_format not in ("plain", "json"):
        raise ValueError(f"Invalid log format {resolved_format!r}; expected 'plain' or 'json'")
    resolved_stream = stream if stream is not None else sys.stderr

    formatter: logging.Formatter
    if resolved_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(_PLAIN_FORMAT)

    handler = logging.StreamHandler(resolved_stream)
    handler.setFormatter(formatter)
    handler.set_name(_HANDLER_NAME)

    root = logging.getLogger("wobblebot")
    root.setLevel(resolved_level)

    # Idempotency: drop any prior handler we installed before adding the new one.
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME]
    root.addHandler(handler)
    # Do not propagate to the stdlib root logger - we own our subtree's output.
    root.propagate = False

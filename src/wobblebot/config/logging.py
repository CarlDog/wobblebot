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
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
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
_ROTATING_HANDLER_NAME = "wobblebot.rotating-file"


def configure_logging(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    level: str | None = None,
    log_format: LogFormat | None = None,
    stream: IO[str] | None = None,
    rotating_file_path: Path | str | None = None,
    rotate_when: str = "midnight",
    rotate_backup_count: int = 7,
) -> None:
    """Configure the root logger for the wobblebot package.

    Args:
        level: Logging level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Defaults to ``$WOBBLEBOT_LOG_LEVEL`` or ``INFO``.
        log_format: ``"plain"`` (human-readable) or ``"json"`` (one
            object per line). Defaults to ``$WOBBLEBOT_LOG_FORMAT`` or
            ``"plain"``.
        stream: Output stream. Defaults to ``sys.stderr``.
        rotating_file_path: Optional path for a rotating-file log
            handler (Stage 8.2.D). When set, a
            ``TimedRotatingFileHandler`` is added alongside the
            stderr stream handler — the operator gets both stdout
            tailability AND a persisted file with retention. Parent
            directory is created if missing.
        rotate_when: Rotation trigger string passed to
            ``TimedRotatingFileHandler``. Default ``"midnight"``
            rotates daily at UTC midnight. ``"H"`` (hourly), ``"D"``
            (daily), ``"W0"``–``"W6"`` (weekly) all supported.
        rotate_backup_count: Number of rotated backups to keep.
            Default 7 = one week of daily rotated logs.

    Idempotent. Calling twice replaces the existing handlers rather
    than stacking new ones.
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

    # Idempotency: drop any prior handlers we installed before
    # adding new ones. Both the stream handler AND the rotating
    # file handler are tracked by named handlers so re-runs replace
    # cleanly. Close() FIRST so the rotating handler's open file
    # descriptor doesn't leak across the replacement.
    kept_handlers = []
    for h in root.handlers:
        if h.get_name() in (_HANDLER_NAME, _ROTATING_HANDLER_NAME):
            try:
                h.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        else:
            kept_handlers.append(h)
    root.handlers = kept_handlers
    root.addHandler(handler)

    # Optional rotating-file handler (Stage 8.2.D). Added ALONGSIDE
    # the stream handler — the operator gets both streams. Accepts
    # str or Path so daemon CLIs can pass `config.<daemon>.log_file_path`
    # directly without coercing per-callsite (added 2026-05-25 when
    # file logging was wired across every daemon).
    if rotating_file_path is not None:
        rotating_path = Path(rotating_file_path)
        rotating_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.TimedRotatingFileHandler(
            filename=str(rotating_path),
            when=rotate_when,
            backupCount=rotate_backup_count,
            encoding="utf-8",
            utc=True,
        )
        rotating.setFormatter(formatter)
        rotating.set_name(_ROTATING_HANDLER_NAME)
        root.addHandler(rotating)

    # Do not propagate to the stdlib root logger - we own our subtree's output.
    root.propagate = False

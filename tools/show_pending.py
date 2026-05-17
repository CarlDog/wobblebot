"""Read-only inspection of persisted pending commands (Stage 5.6.D).

Print recent ``pending_commands`` rows for operator review. Per
ADR-002 + ADR-013 this is purely a read surface — nothing here
executes, approves, rejects, or otherwise mutates state. The
operator inspects what's awaiting confirmation, what was approved
and dispatched, and what was rejected / expired / failed.

Usage::

    python tools/show_pending.py
    python tools/show_pending.py --db-path data/wobblebot-operator.db
    python tools/show_pending.py --status awaiting_confirmation
    python tools/show_pending.py --status approved --limit 5
    python tools/show_pending.py --log-format json

Safe to run against the live operator DB while ``cli/operator`` is
running — SQLite handles concurrent readers; no write surface.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.ports.operator import PendingCommand

_LOGGER = logging.getLogger("wobblebot.tools.show_pending")
_DEFAULT_DB = Path("data") / "wobblebot-operator.db"

_VALID_STATUSES = (
    "awaiting_confirmation",
    "approved",
    "rejected",
    "expired",
    "dispatched",
    "failed",
)


def _format_line(pending: PendingCommand) -> str:
    """One-line human summary."""
    when_iso = pending.created_at.dt.isoformat()
    when_short = when_iso.split("T")[0] + " " + when_iso.split("T")[1].split(".")[0]
    suffix = ""
    if pending.confirming_user_id:
        suffix += f" by {pending.confirming_user_id}"
    if pending.dispatched_at is not None:
        suffix += f" dispatched at {pending.dispatched_at.dt.isoformat()}"
    return " | ".join(
        [
            when_short,
            pending.status,
            pending.command.kind,
            f"channel={pending.channel_id}",
            f"requester={pending.requesting_user_id}",
            f"id={pending.id}",
        ]
    ) + suffix


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        _LOGGER.warning("operator db not found", extra={"db_path": str(db_path)})
        return 0

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        rows = await storage.get_pending_commands(
            status=args.status if args.status else None,
            limit=args.limit,
        )
    finally:
        await storage.close()

    if not rows:
        _LOGGER.info(
            "no pending commands match",
            extra={"status": args.status, "limit": args.limit},
        )
        return 0

    for row in rows:
        _LOGGER.info(_format_line(row))
    _LOGGER.info("matched %d row(s)", len(rows))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools/show_pending.py",
        description="Print persisted pending commands from operator.db.",
    )
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"Operator DB path (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--status",
        choices=_VALID_STATUSES,
        default=None,
        help="Filter to one status (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to return (default: 20)",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Logging format",
    )
    args = parser.parse_args()
    configure_logging(level="INFO", log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

"""Read-only inspection of persisted transfer proposals (Stage 4.3).

Print recent ``transfer_proposals`` rows for operator review. Per
ADR-002 + ADR-003 this is purely a read surface — nothing here
executes, approves, or otherwise touches money. Looking at proposals
is how the operator decides whether to manually transfer or wait
for Stage 4.4's approve+execute path to land.

Usage::

    python tools/show_proposals.py
    python tools/show_proposals.py --db-path data/wobblebot-harvest.db
    python tools/show_proposals.py --since-hours 12 --limit 5
    python tools/show_proposals.py --direction exchange_to_bank
    python tools/show_proposals.py --asset USD --log-format json

Safe to run against the live harvest DB while ``cli/harvest`` is
polling — SQLite handles concurrent readers; no write surface.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging

_LOGGER = logging.getLogger("wobblebot.tools.show_proposals")
_DEFAULT_DB = Path("data") / "wobblebot-harvest.db"


def _format_line(proposal: dict[str, Any]) -> str:
    """One-line human summary for stdout logging."""
    when_iso = _extract_iso(proposal["created_at"])
    when_short = when_iso.split("T")[0] + " " + when_iso.split("T")[1].split(".")[0]
    return " | ".join(
        [
            when_short,
            proposal["direction"],
            f"{proposal['asset']} {proposal['amount']}",
            f"balance: {proposal['current_exchange_balance']} -> "
            f"{proposal['target_exchange_balance']}",
            proposal["rationale"][:80],
        ]
    )


def _extract_iso(value: Any) -> str:
    """Pull an ISO-8601 string out of a Timestamp dump
    (``{"dt": "..."}``) or a bare ISO string. Tolerant of either
    shape so the inspector works against historical rows."""
    if isinstance(value, dict):
        inner = value.get("dt") or ""
        return str(inner)
    return str(value)


def _flatten_for_log(proposal_dict: dict[str, Any]) -> dict[str, Any]:
    """Pull the headline fields up for structured-log emission."""
    return {
        "proposal_id": proposal_dict["proposal_id"],
        "direction": proposal_dict["direction"],
        "asset": proposal_dict["asset"],
        "amount": proposal_dict["amount"],
        "rationale": proposal_dict["rationale"],
        "current_exchange_balance": proposal_dict["current_exchange_balance"],
        "target_exchange_balance": proposal_dict["target_exchange_balance"],
        "created_at": proposal_dict["created_at"],
    }


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        _LOGGER.error("db not found", extra={"db_path": str(db_path)})
        return 2

    since: datetime | None = None
    if args.since_hours is not None:
        since = datetime.now(UTC) - timedelta(hours=args.since_hours)

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        proposals = await storage.get_transfer_proposals(
            since=since,
            direction=args.direction,
            asset=args.asset,
            limit=args.limit,
        )
    finally:
        await storage.close()

    if not proposals:
        _LOGGER.info(
            "no transfer proposals match the filters",
            extra={"db_path": str(db_path)},
        )
        return 0

    for proposal in proposals:
        record = proposal.model_dump(mode="json")
        _LOGGER.info(_format_line(record), extra=_flatten_for_log(record))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"SQLite DB to read (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="Only include proposals newer than N hours.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum proposals to print (default 20).",
    )
    parser.add_argument(
        "--direction",
        choices=("exchange_to_bank", "bank_to_exchange"),
        default=None,
        help="Restrict to one transfer direction.",
    )
    parser.add_argument(
        "--asset",
        default=None,
        help="Restrict to one asset (e.g. USD).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format. json emits one record per proposal with full fields.",
    )
    args = parser.parse_args()

    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

"""Read-only inspection of persisted transfer results (Stage 4.4d).

Print recent ``transfer_results`` rows for operator review. Per
ADR-002 + ADR-003 this is purely a read surface — nothing here
executes, retries, or otherwise touches money. The audit chain:

    propose_transfer()
        → cli/harvest persists TransferProposal (Stage 4.3)
        → operator reviews via tools/show_proposals.py
        → cli/harvest --execute <id> calls Kraken /Withdraw (4.4c)
        → TransferResult persists with refid (status=pending) OR
          status=failed if Kraken refused (4.4c)
        → tools/show_transfers.py shows the outcome (this tool)

Usage::

    python tools/show_transfers.py
    python tools/show_transfers.py --db-path data/wobblebot-harvest.db
    python tools/show_transfers.py --since-hours 168 --limit 50
    python tools/show_transfers.py --status failed
    python tools/show_transfers.py --asset USD --log-format json

Safe to run while ``cli/harvest`` is polling — SQLite handles
concurrent readers; no write surface.
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

_LOGGER = logging.getLogger("wobblebot.tools.show_transfers")
_DEFAULT_DB = Path("data") / "wobblebot-harvest.db"


def _format_line(result: dict[str, Any]) -> str:
    """One-line human summary for stdout logging."""
    when_iso = _extract_iso(result["timestamp"])
    when_short = when_iso.split("T")[0] + " " + when_iso.split("T")[1].split(".")[0]
    return " | ".join(
        [
            when_short,
            result["status"].upper(),
            result["direction"],
            f"{result['asset']} {result['executed_amount']}",
            f"refid={result['transaction_id']}",
        ]
    )


def _extract_iso(value: Any) -> str:
    """Pull an ISO-8601 string out of a Timestamp dump
    (``{"dt": "..."}``) or a bare ISO string."""
    if isinstance(value, dict):
        inner = value.get("dt") or ""
        return str(inner)
    return str(value)


def _flatten_for_log(result_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": result_dict["proposal_id"],
        "transaction_id": result_dict["transaction_id"],
        "status": result_dict["status"],
        "direction": result_dict["direction"],
        "asset": result_dict["asset"],
        "executed_amount": result_dict["executed_amount"],
        "timestamp": result_dict["timestamp"],
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
        results = await storage.get_transfer_results(
            since=since,
            status=args.status,
            asset=args.asset,
            direction=args.direction,
            limit=args.limit,
        )
    finally:
        await storage.close()

    if not results:
        _LOGGER.info(
            "no transfer results match the filters",
            extra={"db_path": str(db_path)},
        )
        return 0

    for result in results:
        record = result.model_dump(mode="json")
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
        help="Only include results newer than N hours.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results to print (default 20).",
    )
    parser.add_argument(
        "--status",
        choices=("pending", "completed", "failed"),
        default=None,
        help="Restrict to one status.",
    )
    parser.add_argument(
        "--direction",
        choices=("exchange_to_bank", "bank_to_exchange"),
        default=None,
        help="Restrict to one direction.",
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
        help="Output format. json emits one record per result with full fields.",
    )
    args = parser.parse_args()

    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

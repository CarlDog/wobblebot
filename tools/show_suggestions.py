"""Read-only inspection of persisted advisor suggestions (Stage 3.3).

Print recent ``advisor_suggestions`` rows for operator review. Per
ADR-002 + ADR-007 this is purely a read surface — nothing here
applies, modifies, or otherwise touches engine config. Looking at
suggestions is how the operator decides whether to manually adjust
grid params in response to advisor input.

Usage::

    python tools/show_suggestions.py
    python tools/show_suggestions.py --db-path data/wobblebot-advise.db
    python tools/show_suggestions.py --since-hours 12 --limit 5
    python tools/show_suggestions.py --model phi4:14b --log-format json
    python tools/show_suggestions.py --role quant

Safe to run against the live advise DB while ``cli/advise`` is
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

_LOGGER = logging.getLogger("wobblebot.tools.show_suggestions")
_DEFAULT_DB = Path("data") / "wobblebot-advise.db"


def _format_line(metrics: dict[str, Any]) -> str:
    """One-line human summary for stdout logging."""
    rec = metrics["recommendation"]
    when_iso = _extract_iso(metrics["created_at"])
    when_short = when_iso.split("T")[0] + " " + when_iso.split("T")[1].split(".")[0]
    confidence = rec["confidence"]
    parts = [
        when_short,
        f"[{metrics['model_name']}]",
        f"role={rec['role']}",
        f"conf={confidence}",
        f"recs={rec['recommendations']}",
    ]
    return " | ".join(parts)


def _extract_iso(value: Any) -> str:
    """Pull an ISO-8601 string out of a Timestamp dump (``{"dt": "..."}``)
    or a bare ISO string. Tolerant of either shape so the inspector works
    against historical rows."""
    if isinstance(value, dict):
        inner = value.get("dt") or ""
        return str(inner)
    return str(value)


def _flatten_for_log(suggestion_dict: dict[str, Any]) -> dict[str, Any]:
    """Pull the headline fields up for structured-log emission."""
    rec = suggestion_dict["recommendation"]
    return {
        "recommendation_id": rec["recommendation_id"],
        "created_at": suggestion_dict["created_at"],
        "model_name": suggestion_dict["model_name"],
        "role": rec["role"],
        "confidence": rec["confidence"],
        "recommendations": rec["recommendations"],
        "rationale": rec["rationale"],
        "input_summary": suggestion_dict["input_summary"],
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
        suggestions = await storage.get_advisor_suggestions(
            since=since,
            model_name=args.model,
            role=args.role,
            limit=args.limit,
        )
    finally:
        await storage.close()

    if not suggestions:
        _LOGGER.info("no advisor suggestions match the filters", extra={"db_path": str(db_path)})
        return 0

    for suggestion in suggestions:
        record = suggestion.model_dump(mode="json")
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
        help="Only include suggestions newer than N hours.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum suggestions to print (default 20).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Restrict to one producing model (e.g. phi4:14b).",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="Restrict to one expert role (single, quant, risk, ...).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format. json emits one record per suggestion with full fields.",
    )
    args = parser.parse_args()

    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

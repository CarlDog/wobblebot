"""Read-only inspection of cloud-LLM cost ledger (Stage 6.1.E, ADR-014).

Print recent ``llm_calls`` rows + optional per-provider / per-role
rollups for operator cost review. Pure read surface — never mutates,
never calls a cloud provider. Safe to run while ``cli/operator`` /
``cli/advise`` are writing rows; SQLite handles concurrent readers.

The forensic ledger lives in ``operator.db`` per ADR-013 (the same
DB that owns ``pending_commands`` + ``notifications`` +
``conversation_turns``). One row per cloud-LLM call regardless of
success / failure — Ollama (local, free) calls don't pass through.

Usage::

    python tools/show_llm_costs.py
    python tools/show_llm_costs.py --since-hours 168 --limit 50
    python tools/show_llm_costs.py --provider anthropic
    python tools/show_llm_costs.py --role quant --since-hours 24
    python tools/show_llm_costs.py --by-provider
    python tools/show_llm_costs.py --by-role --since-hours 24
    python tools/show_llm_costs.py --log-format json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.domain.llm_cost import LLMCallRecord, LLMProvider, LLMRole
from wobblebot.domain.value_objects import Timestamp

_LOGGER = logging.getLogger("wobblebot.tools.show_llm_costs")
_DEFAULT_DB = Path("data") / "wobblebot-operator.db"


_VALID_PROVIDERS: tuple[LLMProvider, ...] = ("anthropic", "openai", "google")
_VALID_ROLES: tuple[LLMRole, ...] = (
    "operator",
    "quant",
    "risk",
    "news",
    "arbitrator",
    "single",
    "unknown",
)


def _format_line(rec: LLMCallRecord) -> str:
    """One-line human summary for stdout logging."""
    when_iso = rec.timestamp.dt.isoformat()
    when_short = when_iso.split("T")[0] + " " + when_iso.split("T")[1].split(".")[0]
    reasoning_part = f" reasoning={rec.tokens_reasoning}" if rec.tokens_reasoning else ""
    status = "OK" if rec.success else f"FAIL({rec.error_kind or '?'})"
    return " | ".join(
        [
            when_short,
            status,
            f"{rec.provider}/{rec.model}",
            f"role={rec.role}",
            f"in={rec.tokens_in} out={rec.tokens_out}{reasoning_part}",
            f"${rec.cost_usd}",
        ]
    )


def _format_rollup(label: str, totals: dict[str, tuple[Decimal, int]]) -> list[str]:
    """Render a rollup table.

    ``totals`` maps a group key (provider / role) to (sum_cost_usd,
    call_count). The output is a list of human-readable rows sorted
    by cost descending, with a final ``total`` row.
    """
    if not totals:
        return [f"{label}: (no rows)"]
    rows = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
    width = max((len(k) for k in totals), default=8)
    lines = [f"{label}:"]
    grand_cost = Decimal("0")
    grand_calls = 0
    for key, (cost, calls) in rows:
        lines.append(f"  {key:<{width}}  ${cost}  ({calls} call{'s' if calls != 1 else ''})")
        grand_cost += cost
        grand_calls += calls
    plural = "s" if grand_calls != 1 else ""
    lines.append(f"  {'total':<{width}}  ${grand_cost}  ({grand_calls} call{plural})")
    return lines


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        _LOGGER.error("operator db not found", extra={"db_path": str(db_path)})
        return 2

    since: Timestamp | None = None
    if args.since_hours is not None:
        since = Timestamp(dt=datetime.now(UTC) - timedelta(hours=args.since_hours))

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        rows = await storage.get_llm_calls(
            since=since,
            role=args.role,
            provider=args.provider,
            limit=args.limit,
        )
    finally:
        await storage.close()

    if not rows:
        _LOGGER.info(
            "no llm_calls match the filters",
            extra={"db_path": str(db_path)},
        )
        return 0

    if args.by_provider:
        by_provider: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))
        for r in rows:
            cost, calls = by_provider[r.provider]
            by_provider[r.provider] = (cost + r.cost_usd, calls + 1)
        for line in _format_rollup("by provider", dict(by_provider)):
            _LOGGER.info(line)
        return 0

    if args.by_role:
        by_role: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (Decimal("0"), 0))
        for r in rows:
            cost, calls = by_role[r.role]
            by_role[r.role] = (cost + r.cost_usd, calls + 1)
        for line in _format_rollup("by role", dict(by_role)):
            _LOGGER.info(line)
        return 0

    # Default mode: per-row print + grand-total footer.
    total = Decimal("0")
    for rec in rows:
        _LOGGER.info(_format_line(rec), extra=rec.model_dump(mode="json"))
        total += rec.cost_usd
    _LOGGER.info("matched %d row(s); total $%s", len(rows), total)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools/show_llm_costs.py",
        description="Print cloud-LLM cost ledger rows from operator.db.",
    )
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"Operator DB path (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="Only include rows newer than N hours.",
    )
    parser.add_argument(
        "--provider",
        choices=_VALID_PROVIDERS,
        default=None,
        help="Restrict to one provider.",
    )
    parser.add_argument(
        "--role",
        choices=_VALID_ROLES,
        default=None,
        help="Restrict to one role.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to read (default 20).",
    )
    rollup = parser.add_mutually_exclusive_group()
    rollup.add_argument(
        "--by-provider",
        action="store_true",
        help="Aggregate by provider (cost + call count) instead of per-row.",
    )
    rollup.add_argument(
        "--by-role",
        action="store_true",
        help="Aggregate by role (cost + call count) instead of per-row.",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format. json emits one record per row with full fields.",
    )
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

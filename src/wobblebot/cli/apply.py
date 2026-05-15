"""Apply CLI — Stage 3.4b bounded auto-tuning gate.

Operator-in-the-loop application of advisor suggestions to the running
grid config. Reads the latest ``AdvisorSuggestion`` from ``advise.db``,
runs it through the ``evaluate_auto_apply`` gate, and prints a
breakdown of which keys would land and which won't (with reasons).

**Dry-run only in Slice B** — no file writes, no DB writes. Slice C
adds ``--commit`` (settings.yml rewriter + AppliedSuggestion audit
row).

Usage::

    python -m wobblebot.cli.apply
    python -m wobblebot.cli.apply --symbol ETH/USD
    python -m wobblebot.cli.apply --recommendation-id rec-abc-123
    python -m wobblebot.cli.apply --config /path/to/custom.yml

Per ADR-002 + ADR-007 this CLI is the only path by which advisor
output mutates running config. Operators read the dry-run output,
decide if it's sane, then re-run with ``--commit`` (Slice C). News-
role suggestions are blanket-rejected here regardless of the
operator's auto_apply.* bounds — that's the load-bearing safety
property the gate exists to enforce.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.advisor import AdvisorSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.auto_apply import AutoApplyResult, evaluate_auto_apply

_LOGGER = logging.getLogger("wobblebot.cli.apply")


def _select_suggestion(
    suggestions: list[AdvisorSuggestion],
    *,
    recommendation_id: str | None,
) -> AdvisorSuggestion | None:
    """Pick the suggestion to evaluate.

    If ``recommendation_id`` is given, find the matching row. Otherwise
    the most recent (``get_advisor_suggestions`` returns DESC by
    ``created_at``) is the default.
    """
    if not suggestions:
        return None
    if recommendation_id is not None:
        for s in suggestions:
            if s.recommendation.recommendation_id == recommendation_id:
                return s
        return None
    return suggestions[0]


def _log_result(suggestion: AdvisorSuggestion, result: AutoApplyResult) -> None:
    """Operator-facing breakdown of a gate evaluation."""
    rec = suggestion.recommendation
    _LOGGER.info(
        "evaluated suggestion",
        extra={
            "recommendation_id": rec.recommendation_id,
            "created_at": suggestion.created_at.dt.isoformat(),
            "model_name": suggestion.model_name,
            "role": rec.role,
            "confidence": rec.confidence,
            "symbol": result.symbol,
            "auto_apply_enabled": result.enabled,
            "role_eligible": result.role_eligible,
            "applied_count": len(result.applied_keys),
            "rejected_count": len(result.rejected_keys),
        },
    )
    for applied in result.applied_keys:
        _LOGGER.info(
            "APPLIED  %s: %s -> %s (%+.2f%%)",
            applied.key,
            applied.before,
            applied.after,
            applied.delta_pct,
            extra={
                "outcome": "applied",
                "key": applied.key,
                "before": applied.before,
                "after": applied.after,
                "delta_pct": applied.delta_pct,
            },
        )
    for rejected in result.rejected_keys:
        _LOGGER.info(
            "REJECTED %s: %r — %s",
            rejected.key,
            rejected.proposed,
            rejected.reason,
            extra={
                "outcome": "rejected",
                "key": rejected.key,
                "proposed": rejected.proposed,
                "reason": rejected.reason,
            },
        )
    if not result.applied_keys and not result.rejected_keys:
        _LOGGER.info(
            "suggestion carried no proposed keys to evaluate",
            extra={"recommendation_id": rec.recommendation_id},
        )


async def _run(args: argparse.Namespace, config: WobbleBotConfig) -> int:
    if config.advise is None:
        _LOGGER.error("settings.yml is missing the `advise:` section")
        return 2
    if config.advisor is None:
        _LOGGER.error("settings.yml is missing the `advisor:` section")
        return 2

    symbol: Symbol
    if args.symbol is not None:
        symbol = Symbol.from_string(args.symbol)
    else:
        symbol = config.advise.symbol

    advise_db = args.db if args.db is not None else config.advise.db
    storage = SQLiteStorageAdapter(advise_db)
    try:
        await storage.connect()
    except StorageError as exc:
        _LOGGER.error(
            "failed to open advise db",
            extra={"path": advise_db, "error": str(exc)},
        )
        return 2

    try:
        suggestions = await storage.get_advisor_suggestions(limit=args.search_limit)
    finally:
        await storage.close()

    suggestion = _select_suggestion(suggestions, recommendation_id=args.recommendation_id)
    if suggestion is None:
        if args.recommendation_id is not None:
            _LOGGER.error(
                "no suggestion matched recommendation-id",
                extra={
                    "recommendation_id": args.recommendation_id,
                    "searched": len(suggestions),
                },
            )
        else:
            _LOGGER.error(
                "no advisor suggestions found in db",
                extra={"db": advise_db},
            )
        return 2

    current_grid = config.grid.for_coin(symbol.base)
    result = evaluate_auto_apply(
        suggestion,
        current_grid,
        config.advisor.auto_apply,
        symbol=symbol.base,
    )
    _log_result(suggestion, result)

    _LOGGER.info(
        "dry-run complete (no file writes; pass --commit in Slice C to apply)",
        extra={
            "would_apply": [a.key for a in result.applied_keys],
            "would_skip": [r.key for r in result.rejected_keys],
        },
    )
    return 0


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "advise",
        {
            "db": ("db", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbol",
        default=None,
        help="Coin to evaluate against (BASE/QUOTE). Defaults to advise.symbol.",
    )
    parser.add_argument(
        "--recommendation-id",
        default=None,
        help="Evaluate a specific suggestion by its recommendation_id. "
        "Default: the most recent suggestion in advise.db.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to advise.db. Defaults to advise.db setting.",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=50,
        help="How many recent suggestions to scan when matching "
        "--recommendation-id. Default 50.",
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = (
        args.log_format
        if args.log_format is not None
        else (config.advise.log_format if config.advise else "plain")
    )
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_run(args, config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

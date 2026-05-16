"""Apply CLI — Stage 3.4b bounded auto-tuning gate.

Operator-in-the-loop application of advisor suggestions to the running
grid config. Reads the latest ``AdvisorSuggestion`` from ``advise.db``,
runs it through the ``evaluate_auto_apply`` gate, prints a breakdown
of which keys would land and which won't (with reasons), and — with
``--commit`` — rewrites ``settings.yml`` and writes a forensic
``AppliedSuggestion`` audit row.

Without ``--commit`` the CLI is read-only: no file writes, no DB
writes. The dry-run mode is the operator's feedback loop before
mutating any state.

Usage::

    python -m wobblebot.cli.apply                          # dry-run
    python -m wobblebot.cli.apply --commit                 # apply
    python -m wobblebot.cli.apply --symbol ETH/USD
    python -m wobblebot.cli.apply --recommendation-id rec-abc-123
    python -m wobblebot.cli.apply --config /path/to/custom.yml

Per ADR-002 + ADR-007 this CLI is the only path by which advisor
output mutates running config. News-role suggestions are blanket-
rejected at the gate regardless of the operator's auto_apply.*
bounds — the load-bearing safety property the gate exists to
enforce.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorSuggestion, AppliedSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.auto_apply import AutoApplyResult, evaluate_auto_apply
from wobblebot.services.settings_rewriter import SettingsRewriteError, apply_grid_overrides

_LOGGER = logging.getLogger("wobblebot.cli.apply")


def _select_suggestion(
    suggestions: list[AdvisorSuggestion],
    *,
    recommendation_id: str | None,
    symbol: str | None = None,
) -> AdvisorSuggestion | None:
    """Pick the suggestion to evaluate.

    Priority:
    1. If ``recommendation_id`` is given, find the matching row. Symbol
       filter is ignored — the operator picked an exact row by ID.
    2. If ``symbol`` is given, return the newest suggestion whose
       ``input_summary["symbol"]`` matches (BASE/QUOTE shape, e.g.
       ``"BTC/USD"``). Stage 3.6b's multi-symbol advise daemon writes
       one row per coin per sweep, so a global "newest" can pick the
       wrong coin.
    3. Otherwise fall back to the newest row overall (Stage 3.4b
       single-coin behavior).
    """
    if not suggestions:
        return None
    if recommendation_id is not None:
        for s in suggestions:
            if s.recommendation.recommendation_id == recommendation_id:
                return s
        return None
    if symbol is not None:
        for s in suggestions:
            if s.input_summary.get("symbol") == symbol:
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


async def _run(  # pylint: disable=too-many-return-statements
    args: argparse.Namespace, config: WobbleBotConfig
) -> int:
    if config.advise is None:
        _LOGGER.error("settings.yml is missing the `advise:` section")
        return 2
    if config.advisor is None:
        _LOGGER.error("settings.yml is missing the `advisor:` section")
        return 2

    symbol: Symbol
    if args.symbol is not None:
        symbol = Symbol.from_string(args.symbol)
    elif config.advise.symbols:
        # Multi-symbol advise daemons (Stage 3.6b) — default to the first
        # configured symbol when --symbol is omitted. Operators with
        # multi-coin coverage typically want explicit --symbol per
        # invocation; the default keeps single-coin operators friction-free.
        symbol = config.advise.symbols[0]
    else:
        _LOGGER.error("settings.yml advise.symbols is empty; cannot infer default")
        return 2

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

    # Filter to suggestions tagged with the target symbol. Stage 3.6b's
    # multi-symbol advise daemon writes one row per coin per sweep, so
    # the newest row in advise.db may be for a different coin than the
    # operator asked about. `--recommendation-id` skips the symbol
    # filter (operator picked an exact row by hand).
    symbol_filter = None if args.recommendation_id is not None else str(symbol)
    suggestion = _select_suggestion(
        suggestions,
        recommendation_id=args.recommendation_id,
        symbol=symbol_filter,
    )
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
                "no advisor suggestions found in db for symbol",
                extra={"db": advise_db, "symbol": str(symbol)},
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

    if not args.commit:
        _LOGGER.info(
            "dry-run complete (no file writes; pass --commit to apply)",
            extra={
                "would_apply": [a.key for a in result.applied_keys],
                "would_skip": [r.key for r in result.rejected_keys],
            },
        )
        return 0

    return await _commit_apply(
        suggestion=suggestion,
        result=result,
        settings_path=Path(args.settings_path or _resolved_settings_path(args)),
        advise_db=advise_db,
    )


async def _commit_apply(
    *,
    suggestion: AdvisorSuggestion,
    result: AutoApplyResult,
    settings_path: Path,
    advise_db: str,
) -> int:
    """Persist a clean apply: rewrite settings.yml + write audit row.

    Refuses to write if no keys applied (nothing to do).
    """
    if not result.applied_keys:
        _LOGGER.warning(
            "no keys cleared the gate; settings.yml NOT modified",
            extra={"recommendation_id": suggestion.recommendation.recommendation_id},
        )
        return 1

    overrides: dict[str, Any] = {a.key: a.after for a in result.applied_keys}
    try:
        diff = apply_grid_overrides(
            settings_path,
            symbol=result.symbol,
            overrides=overrides,
        )
    except (SettingsRewriteError, FileNotFoundError) as exc:
        _LOGGER.error(
            "settings.yml rewrite failed; no audit row written",
            extra={"path": str(settings_path), "error": str(exc)},
        )
        return 1
    if diff:
        # Multi-line print path: log line for the operator, full diff to stdout.
        _LOGGER.info(
            "settings.yml updated",
            extra={"path": str(settings_path), "lines_changed": diff.count("\n")},
        )
        sys.stdout.write(diff)
        sys.stdout.flush()
    else:
        _LOGGER.info(
            "settings.yml unchanged (overrides matched existing values)",
            extra={"path": str(settings_path)},
        )

    storage = SQLiteStorageAdapter(advise_db)
    await storage.connect()
    try:
        applied_row = AppliedSuggestion(
            recommendation_id=suggestion.recommendation.recommendation_id,
            applied_at=Timestamp(dt=datetime.now(UTC)),
            symbol=result.symbol,
            applied_keys=[a.model_dump() for a in result.applied_keys],
            rejected_keys=[r.model_dump() for r in result.rejected_keys],
            model_name=suggestion.model_name,
            rationale=suggestion.recommendation.rationale,
        )
        await storage.save_applied_suggestion(applied_row)
    finally:
        await storage.close()

    _LOGGER.info(
        "commit complete",
        extra={
            "recommendation_id": suggestion.recommendation.recommendation_id,
            "applied_keys": [a.key for a in result.applied_keys],
            "symbol": result.symbol,
        },
    )
    return 0


def _resolved_settings_path(args: argparse.Namespace) -> str:
    """Resolve which settings.yml path the rewriter should target.

    Mirrors the load_resolved_config discovery order: explicit
    --config wins; otherwise the conventional location.
    """
    if args.config is not None:
        return str(args.config)
    default = Path("config") / "settings.yml"
    return str(default)


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
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply the change: rewrite settings.yml in place and "
        "persist an AppliedSuggestion audit row. Without this flag "
        "the CLI runs in dry-run mode (read-only).",
    )
    parser.add_argument(
        "--settings-path",
        default=None,
        help="Path to settings.yml to rewrite on --commit. Defaults to "
        "--config if provided, else config/settings.yml.",
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

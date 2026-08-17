"""Score advisor suggestions against their counterfactual (ADR-035, P4.2).

For each unscored suggestion at the requested granularity, build the two
arms — the config in force at emission (``input_summary.current_grid``)
and that config with the recommendation's replayable keys applied —
replay BOTH through the real ``GridEngine`` over the suggestion's 7-day
forward window (the ADR-028 auditor internals, ``max_daily_spend_usd``
neutered in both arms), and persist the SIGN of the difference to
``recommendation_outcomes``. Rank and hit-rate downstream; never dollars
(ADR-035 decision 3).

Unscoreable suggestions (empty recommendation, keys outside the
replayable surface, missing ``current_grid``, non-numeric or
floor-violating proposed values) are RECORDED with their reason, never
silently skipped (decision 5) — those are permanent facts about the
suggestion and its window. Two conditions leave a suggestion IN THE
QUEUE instead, because they are temporary facts about this machine:
a window that has not fully elapsed (pending), and bars not yet
imported for the window (bars-missing — import the dump, re-run, and
the same rows score at the same evaluator version).

Resumable + idempotent: the queue is ``get_unscored_suggestions``, and
``UNIQUE(suggestion_id, granularity_minutes, evaluator_version)`` makes
a re-run a no-op for every already-written row.

The ratified scoring policy (docs/planning/p4-outcome-ledger-design.md)
is two config-rec passes plus the directional pass (P4.4b)::

    python tools/score_recommendations.py --interval 60m
    python tools/score_recommendations.py --interval 1m --symbols BTC/USD
    python tools/score_recommendations.py --directional

Each pass owns one granularity namespace in the ledger (60 / 1 / NULL)
and SKIPS suggestions of the other kind without writing anything, so a
directional call never consumes a config slot and vice versa. Bounded
probe / smoke run::

    python tools/score_recommendations.py --interval 60m --limit 25
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # direct `python tools/...` execution
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
# (When linting this file ad hoc, run with PYTHONPATH=. so pylint
# resolves tools.auditor the same way pytest and direct execution do.)
from tools.auditor import NEUTERED_DAILY_SPEND, SymbolAuditResult, replay_symbol
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    load_operator_env,
    parse_interval_arg,
    parse_symbol_csv,
)
from wobblebot.config.grid import GridConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.advisor import AdvisorSuggestion, RecommendationOutcome
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.advisor_evaluator import (
    DIRECTIONAL_GRADING_INTERVAL_MINUTES,
    EVALUATOR_VERSION,
    ArmBuildError,
    Classification,
    bar_coverage_reason,
    build_arms,
    build_directional_scored,
    build_scored,
    build_unscoreable,
    classify,
    directional_prices,
    fee_rates_for,
    grade_directional,
    outcome_sign,
)

_LOGGER = logging.getLogger("wobblebot.tools.score_recommendations")

_DEFAULT_ADVISE_DB = Path("data") / "wobblebot-advise.db"
_DEFAULT_BARS_DB = Path("data") / "wobblebot-observe.db"


@dataclass
class RunStats:
    """One run's tallies — the closing summary's source, and the test seam."""

    scored: Counter[str] = field(default_factory=Counter)
    unscoreable: Counter[str] = field(default_factory=Counter)
    pending: int = 0
    filtered: int = 0
    bars_missing: int = 0
    errors: int = 0
    skipped_other_kind: int = 0

    @property
    def processed(self) -> int:
        """Rows actually written this run (scored + unscoreable)."""
        return sum(self.scored.values()) + sum(self.unscoreable.values())


def _arm_json(
    result: SymbolAuditResult,
    config: GridConfig,
    *,
    fee_rate: Decimal,
    seed_usd: Decimal,
    seed_base: Decimal,
) -> dict[str, Any]:
    """Audit summary of one replay arm.

    Floats, not Decimals — these are directional replay summaries for
    the forensic record, not accounting. The dollar-ish fields stay
    internal to the ledger; the scoreboard never surfaces them
    (ADR-035 decision 3).
    """
    levels = config.default
    return {
        "config": {
            "spacing_percentage": float(levels.spacing_percentage),
            "levels_above": levels.levels_above,
            "levels_below": levels.levels_below,
            "order_size_usd": float(levels.order_size_usd),
        },
        "bars_replayed": result.bars_replayed,
        "buys": result.buys,
        "sells": result.sells,
        "cycle_count": result.cycle_count,
        "win_rate": float(result.win_rate),
        "fees_usd": float(result.fees),
        "realized_pnl_usd": float(result.realized_pnl),
        "net_pnl_usd": float(result.net_pnl_usd),
        "max_drawdown": float(result.max_drawdown),
        "refusals": result.refusals,
        "sells_deferred": result.sells_deferred,
        "fee_rate": float(fee_rate),
        "seed_usd": float(seed_usd),
        "seed_base": float(seed_base),
    }


async def _evaluate_directional(
    bars_storage: SQLiteStorageAdapter,
    row_id: int,
    suggestion: AdvisorSuggestion,
    classification: Classification,
) -> RecommendationOutcome | str:
    """Grade one directional call (P4.4b). ``str`` = bars-missing."""
    if classification.unscoreable_reason is not None:
        return build_unscoreable(
            row_id,
            classification,
            granularity_minutes=None,
            reason=classification.unscoreable_reason,
        )
    symbol = classification.symbol
    if symbol is None:  # classify() sets a reason whenever symbol is None
        return build_unscoreable(
            row_id, classification, granularity_minutes=None, reason="input_summary has no symbol"
        )
    interval = timedelta(minutes=DIRECTIONAL_GRADING_INTERVAL_MINUTES)
    bars = await bars_storage.get_ohlc_bars(
        symbol,
        DIRECTIONAL_GRADING_INTERVAL_MINUTES,
        start_time=classification.window_start - 4 * interval,
        end_time=classification.window_end,
    )
    prices = directional_prices(
        bars,
        window_start=classification.window_start,
        window_end=classification.window_end,
    )
    if isinstance(prices, str):
        return prices
    start_price, end_price = prices
    recs = suggestion.recommendation.recommendations
    direction = str(recs["direction"])
    return build_directional_scored(
        row_id,
        classification,
        direction=direction,
        horizon_hours=float(recs["horizon_hours"]),
        start_price=start_price,
        end_price=end_price,
        outcome=grade_directional(direction, start_price, end_price),
    )


async def _evaluate_one(  # pylint: disable=too-many-arguments,too-many-locals
    bars_storage: SQLiteStorageAdapter,
    row_id: int,
    suggestion: AdvisorSuggestion,
    classification: Classification,
    *,
    interval_minutes: int,
    safety_config: SafetyConfig,
    seed_usd: Decimal,
    seed_base: Decimal,
) -> RecommendationOutcome | str:
    """Turn one CONFIG-REC queue entry into its outcome row.

    A plain ``str`` return is the bars-missing signal: the reason the
    window's bars don't cover it, meaning "write nothing, leave it in
    the queue for after the import."
    """
    if classification.unscoreable_reason is not None:
        return build_unscoreable(
            row_id,
            classification,
            granularity_minutes=interval_minutes,
            reason=classification.unscoreable_reason,
        )
    symbol = classification.symbol
    if symbol is None:  # classify() sets a reason whenever symbol is None
        return build_unscoreable(
            row_id,
            classification,
            granularity_minutes=interval_minutes,
            reason="input_summary has no symbol",
        )
    maker_rate, taker_rate = fee_rates_for(classification.window_start)
    try:
        inforce, proposed = build_arms(suggestion, maker_fee_rate=maker_rate)
    except ArmBuildError as exc:
        return build_unscoreable(
            row_id, classification, granularity_minutes=interval_minutes, reason=str(exc)
        )
    bars = await bars_storage.get_ohlc_bars(
        symbol,
        interval_minutes,
        start_time=classification.window_start,
        end_time=classification.window_end,
    )
    coverage_reason = bar_coverage_reason(
        bars,
        window_start=classification.window_start,
        window_end=classification.window_end,
        interval_minutes=interval_minutes,
    )
    if coverage_reason is not None:
        return coverage_reason

    inforce_result = await replay_symbol(
        bars,
        symbol,
        grid_config=inforce,
        safety_config=safety_config,
        seed_usd=seed_usd,
        seed_base=seed_base,
        fee_rate=maker_rate,
        maker_fee_rate=maker_rate,
        taker_fee_rate=taker_rate,
    )
    if proposed == inforce:
        # Proposed values equal the in-force values: the arms are the
        # same config, the sign is 0 by construction — one replay, tie.
        proposed_result = inforce_result
    else:
        proposed_result = await replay_symbol(
            bars,
            symbol,
            grid_config=proposed,
            safety_config=safety_config,
            seed_usd=seed_usd,
            seed_base=seed_base,
            fee_rate=maker_rate,
            maker_fee_rate=maker_rate,
            taker_fee_rate=taker_rate,
        )
    return build_scored(
        row_id,
        classification,
        granularity_minutes=interval_minutes,
        proposed_arm=_arm_json(
            proposed_result, proposed, fee_rate=maker_rate, seed_usd=seed_usd, seed_base=seed_base
        ),
        inforce_arm=_arm_json(
            inforce_result, inforce, fee_rate=maker_rate, seed_usd=seed_usd, seed_base=seed_base
        ),
        outcome=outcome_sign(proposed_result.net_pnl_usd, inforce_result.net_pnl_usd),
    )


def _pass_label(interval_minutes: int | None) -> str:
    return "directional" if interval_minutes is None else f"{interval_minutes}m"


async def score_corpus(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    advise_storage: SQLiteStorageAdapter,
    bars_storage: SQLiteStorageAdapter,
    *,
    interval_minutes: int | None,
    safety_config: SafetyConfig,
    symbols: set[Symbol] | None = None,
    limit: int | None = None,
    seed_usd: Decimal = Decimal("10000"),
    seed_base: Decimal = Decimal("0"),
    now: datetime | None = None,
) -> RunStats:
    """Drain the unscored queue at one granularity. The driver's core.

    ``interval_minutes=None`` is the DIRECTIONAL pass (the ledger's
    NULL-granularity namespace); an int is a config-rec pass. Each pass
    skips the other kind's suggestions without writing, so the two
    namespaces stay independent. ``symbols`` filters the queue (e.g.
    the BTC-only 1m pass); ``limit`` caps rows WRITTEN this run
    (pending/filtered/skipped entries don't consume it); ``now`` is the
    pending-window cutoff, injectable for tests.
    """
    current_time = now if now is not None else datetime.now(UTC)
    neutered = safety_config.model_copy(update={"max_daily_spend_usd": NEUTERED_DAILY_SPEND})
    queue = await advise_storage.get_unscored_suggestions(interval_minutes, EVALUATOR_VERSION)
    _LOGGER.info(
        "scoring queue: %d unscored suggestions at %s, evaluator v%d "
        "(config-rec arms neutered per ADR-028 correction 1; directional rows replay nothing)",
        len(queue),
        _pass_label(interval_minutes),
        EVALUATOR_VERSION,
    )
    stats = RunStats()
    for row_id, suggestion in queue:
        if limit is not None and stats.processed >= limit:
            break
        classification = classify(suggestion)
        if (classification.kind == "directional_call") != (interval_minutes is None):
            stats.skipped_other_kind += 1
            continue
        if symbols is not None and (
            classification.symbol is None or classification.symbol not in symbols
        ):
            stats.filtered += 1
            continue
        if classification.window_end > current_time:
            stats.pending += 1
            continue
        try:
            if interval_minutes is None:
                outcome_row = await _evaluate_directional(
                    bars_storage, row_id, suggestion, classification
                )
            else:
                outcome_row = await _evaluate_one(
                    bars_storage,
                    row_id,
                    suggestion,
                    classification,
                    interval_minutes=interval_minutes,
                    safety_config=neutered,
                    seed_usd=seed_usd,
                    seed_base=seed_base,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # A 2,862-item batch must not wedge on one poisoned row —
            # but the failure stays LOUD: per-row ERROR here, a nonzero
            # errors tally in the summary, and exit code 1 from the
            # CLI. The row writes nothing, so a fixed evaluator rescores
            # it at the same version.
            stats.errors += 1
            _LOGGER.error("suggestion #%d: evaluation failed, left in queue: %s", row_id, exc)
            continue
        if isinstance(outcome_row, str):
            stats.bars_missing += 1
            _LOGGER.debug("bars missing for #%d, left in queue: %s", row_id, outcome_row)
            continue
        try:
            await advise_storage.save_recommendation_outcome(outcome_row)
        except StorageError as exc:
            _LOGGER.warning("suggestion #%d: outcome save failed, skipping (%s)", row_id, exc)
            continue
        if outcome_row.outcome is not None:
            stats.scored[outcome_row.outcome] += 1
            _LOGGER.info(
                "scored #%d role=%s %s @%s window %s -> %s: %s",
                row_id,
                suggestion.recommendation.role,
                classification.symbol,
                _pass_label(interval_minutes),
                classification.window_start.date().isoformat(),
                classification.window_end.date().isoformat(),
                outcome_row.outcome,
            )
        else:
            reason = outcome_row.unscoreable_reason or "unknown"
            stats.unscoreable[reason.split(":", 1)[0]] += 1
            _LOGGER.debug("unscoreable #%d: %s", row_id, reason)
    _log_summary(stats, len(queue))
    return stats


def _log_summary(stats: RunStats, queue_size: int) -> None:
    """Closing tallies (message-first per logging conventions)."""
    _LOGGER.info(
        "run complete: %d scored (better %d / worse %d / tie %d); %d unscoreable; "
        "%d pending (window not yet elapsed); %d bars-missing (left in queue — "
        "import bars and re-run); %d errors; %d filtered by --symbols; "
        "%d skipped (other kind — scored by their own pass); queue was %d",
        sum(stats.scored.values()),
        stats.scored["better"],
        stats.scored["worse"],
        stats.scored["tie"],
        sum(stats.unscoreable.values()),
        stats.pending,
        stats.bars_missing,
        stats.errors,
        stats.filtered,
        stats.skipped_other_kind,
        queue_size,
    )
    for reason, count in stats.unscoreable.most_common():
        _LOGGER.info("  unscoreable x%d: %s", count, reason)


async def _run(args: argparse.Namespace) -> int:
    try:
        config = load_resolved_config(config_path=args.config, profile_name=args.profile)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        _LOGGER.error("config load failed: %s", exc)
        return 2

    symbols: set[Symbol] | None = None
    if args.symbols:
        try:
            symbols = {Symbol.from_string(s) for s in parse_symbol_csv(args.symbols)}
        except ValueError as exc:
            _LOGGER.error("bad --symbols: %s", exc)
            return 2
        if not symbols:
            _LOGGER.error("--symbols resolved to an empty list")
            return 2

    advise_storage = SQLiteStorageAdapter(str(args.db))
    bars_storage = SQLiteStorageAdapter(str(args.bars_db))
    await advise_storage.connect()
    try:
        await bars_storage.connect()
        try:
            stats = await score_corpus(
                advise_storage,
                bars_storage,
                interval_minutes=None if args.directional else args.interval,
                safety_config=config.safety,
                symbols=symbols,
                limit=args.limit,
                seed_usd=args.seed_usd,
                seed_base=args.seed_base,
            )
            return 1 if stats.errors else 0
        finally:
            await bars_storage.close()
    finally:
        await advise_storage.close()


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--db",
        default=str(_DEFAULT_ADVISE_DB),
        help="Advise DB holding advisor_suggestions + recommendation_outcomes.",
    )
    parser.add_argument(
        "--bars-db",
        default=str(_DEFAULT_BARS_DB),
        help="Observe DB holding the ohlc_bars the windows replay over.",
    )
    parser.add_argument(
        "--interval",
        type=parse_interval_arg,
        default=60,
        help="Replay granularity. Ratified policy: 60m corpus-wide, 1m for the BTC cross-check.",
    )
    parser.add_argument(
        "--directional",
        action="store_true",
        help=(
            "Run the directional-call pass (the ledger's NULL-granularity "
            "namespace) instead of a config-rec pass; --interval is ignored."
        ),
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Optional comma-separated filter (e.g. BTC/USD for the 1m pass).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on outcome rows written this run (bounded probe; resume by re-running).",
    )
    parser.add_argument(
        "--seed-usd",
        type=Decimal,
        default=Decimal("10000"),
        help="Starting quote balance for BOTH arms (identical, so it cancels — ADR-035 d.2).",
    )
    parser.add_argument(
        "--seed-base",
        type=Decimal,
        default=Decimal("0"),
        help="Starting base-asset balance for both arms.",
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

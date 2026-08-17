"""Advisor outcome scoreboard — read-only ledger report (ADR-035, P4.3).

Renders the ``recommendation_outcomes`` ledger as a log-table (screener
precedent; a web card can reuse ``services/outcome_scoreboard.py``
later): per-role / per-granularity scored counts with hit-rate, the
unscoreable taxonomy, the unscored-queue remainders, and the paired
quant-vs-heuristic comparison on the escalated subset (ADR-035
decision 7).

Framing rules, enforced by the aggregation module and repeated in the
output: each role is measured against its OWN in-force counterfactual —
this is the cascade against itself, never an inter-role ranking; a
hit-rate prints only at or above 30 decisive rows, always with its
sample; counts and rates only, never dollars.

The pairing re-runs the CURRENT deterministic guard layer over each
scored quant row's stored ``input_summary``. Escalation means the guard
layer held, so its would-have-said is expected to be HOLD — but the
premise is verified per row, not assumed: rows where a guard fires on
re-run (spec drift since emission) are excluded and counted, as are
summaries that no longer parse.

Usage::

    python tools/score_report.py
    python tools/score_report.py --db data/wobblebot-advise.db --evaluator-version 1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, load_operator_env
from wobblebot.config.heuristic import HeuristicSpec, load_heuristic_spec
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.advisor import (
    PerformanceSummary,
    RecommendationOutcome,
)
from wobblebot.services.advisor_evaluator import EVALUATOR_VERSION
from wobblebot.services.outcome_scoreboard import (
    MIN_SAMPLE_FOR_RATE,
    ScoreboardCell,
    aggregate_scored,
    pair_quant_vs_hold,
    unscoreable_taxonomy,
)

_LOGGER = logging.getLogger("wobblebot.tools.score_report")

_DEFAULT_ADVISE_DB = Path("data") / "wobblebot-advise.db"

# The ratified scoring granularities (p4-outcome-ledger-design.md);
# queue remainders are always reported for these even when the ledger
# holds no rows at one of them yet.
_RATIFIED_GRANULARITIES = (60, 1)


@dataclass
class PairingStats:
    """Paired quant-vs-hold tallies for one granularity."""

    quant_wins: int = 0
    heuristic_wins: int = 0
    even: int = 0
    guard_fired: Counter[str] = field(default_factory=Counter)
    unparseable: int = 0

    @property
    def held(self) -> int:
        """Rows where the re-run guard layer held (the paired subset)."""
        return self.quant_wins + self.heuristic_wins + self.even

    @property
    def rerun(self) -> int:
        """All rows the pairing attempted."""
        return self.held + sum(self.guard_fired.values()) + self.unparseable


@dataclass
class Report:  # pylint: disable=too-many-instance-attributes
    """Everything the renderer needs; the test seam."""

    total_outcomes: int
    scored_count: int
    unscoreable_count: int
    cells: list[ScoreboardCell]
    taxonomy: Counter[str]
    queue_remainders: dict[int | None, int]
    pairing: dict[int | None, PairingStats] | None
    pairing_skip_reason: str | None


def _pair_escalated(
    scored: list[tuple[str, RecommendationOutcome, dict[str, object]]],
    adapter: HeuristicAdvisorAdapter,
) -> dict[int | None, PairingStats]:
    """Re-run the guard layer over each scored quant row's input."""
    pairing: dict[int | None, PairingStats] = {}
    for role, outcome, summary_dict in scored:
        if role != "quant" or outcome.outcome is None:
            continue
        stats = pairing.setdefault(outcome.granularity_minutes, PairingStats())
        try:
            summary = PerformanceSummary.model_validate(summary_dict)
        except ValidationError:
            stats.unparseable += 1
            continue
        verdict = adapter.evaluate(summary)
        if verdict.recommendation.recommendations:
            stats.guard_fired[verdict.reason] += 1
            continue
        winner = pair_quant_vs_hold(outcome.outcome)
        if winner == "quant":
            stats.quant_wins += 1
        elif winner == "heuristic":
            stats.heuristic_wins += 1
        else:
            stats.even += 1
    return pairing


async def build_report(  # pylint: disable=too-many-locals
    storage: SQLiteStorageAdapter,
    *,
    evaluator_version: int,
    heuristic_spec: HeuristicSpec | None,
    pairing_skip_reason: str | None = None,
) -> Report:
    """Assemble the scoreboard from one advise DB."""
    outcomes = await storage.get_recommendation_outcomes(evaluator_version=evaluator_version)
    scored_rows = [o for o in outcomes if o.scoreable and o.outcome is not None]
    unscoreable_rows = [o for o in outcomes if not o.scoreable]

    ids = sorted({o.suggestion_id for o in scored_rows})
    suggestions = dict(await storage.get_advisor_suggestions_by_ids(ids))
    joined = [
        (suggestions[o.suggestion_id].recommendation.role, o, suggestions[o.suggestion_id])
        for o in scored_rows
        if o.suggestion_id in suggestions
    ]
    cells = aggregate_scored((role, outcome) for role, outcome, _ in joined)

    granularities = set(_RATIFIED_GRANULARITIES) | {o.granularity_minutes for o in outcomes}
    queue_remainders = {
        g: len(await storage.get_unscored_suggestions(g, evaluator_version))
        for g in sorted(granularities, key=lambda g: (g is None, g if g is not None else 0))
    }

    pairing: dict[int | None, PairingStats] | None = None
    if heuristic_spec is not None:
        adapter = HeuristicAdvisorAdapter(spec=heuristic_spec)
        pairing = _pair_escalated(
            [(role, outcome, s.input_summary) for role, outcome, s in joined],
            adapter,
        )

    return Report(
        total_outcomes=len(outcomes),
        scored_count=len(scored_rows),
        unscoreable_count=len(unscoreable_rows),
        cells=cells,
        taxonomy=unscoreable_taxonomy(unscoreable_rows),
        queue_remainders=queue_remainders,
        pairing=pairing,
        pairing_skip_reason=pairing_skip_reason,
    )


def _granularity_label(granularity: int | None) -> str:
    return "directional-call" if granularity is None else f"{granularity}m"


def _render(report: Report, evaluator_version: int) -> None:
    """Log-table output (message-first per logging conventions)."""
    _LOGGER.info("advisor outcome scoreboard -- evaluator v%d", evaluator_version)
    _LOGGER.info(
        "framing: each role is scored against ITS OWN in-force counterfactual "
        "(ADR-035) -- the cascade measured against itself, never an inter-role "
        "ranking; counts and rates only, never dollars"
    )
    _LOGGER.info(
        "ledger: %d outcome rows (%d scored / %d unscoreable)",
        report.total_outcomes,
        report.scored_count,
        report.unscoreable_count,
    )
    if report.cells:
        _LOGGER.info(
            "scored (hit-rate = better/decisive; rate withheld below %d decisive rows):",
            MIN_SAMPLE_FOR_RATE,
        )
        for cell in report.cells:
            if cell.hit_rate is not None:
                rate = f"hit-rate {cell.hit_rate * 100:.0f}% of {cell.decisive} decisive"
            else:
                rate = f"decisive {cell.decisive} -- below the {MIN_SAMPLE_FOR_RATE}-row floor"
            _LOGGER.info(
                "  %s @%s: %d scored -- better %d / worse %d / tie %d (%s)",
                cell.role,
                _granularity_label(cell.granularity_minutes),
                cell.scored,
                cell.better,
                cell.worse,
                cell.tie,
                rate,
            )
        _LOGGER.info(
            "  fidelity note: 60m rows are directional (ADR-028 -- intra-bar "
            "ordering unknowable); 1m rows are _Sim-equivalent"
        )
    else:
        _LOGGER.info("scored: none yet -- the ledger has no scored rows at this version")
    if report.taxonomy:
        _LOGGER.info("unscoreable (reasons collapsed to their first clause):")
        for reason, count in report.taxonomy.most_common():
            _LOGGER.info("  x%d %s", count, reason)
    for granularity, remaining in report.queue_remainders.items():
        _LOGGER.info(
            "queue @%s: %d suggestions unscored (pending windows + bars-missing; "
            "tools/score_recommendations.py drains this)",
            _granularity_label(granularity),
            remaining,
        )
    _render_pairing(report)


def _render_pairing(report: Report) -> None:
    if report.pairing is None:
        _LOGGER.warning("pairing skipped: %s", report.pairing_skip_reason or "no heuristic spec")
        return
    _LOGGER.info(
        "paired: quant vs the guard layer's would-have-said, escalated subset "
        "only (ADR-035 d.7) -- the ONE legitimate head-to-head; a hold keeps "
        "the in-force config, so on held rows the pairing IS the outcome sign"
    )
    if not report.pairing:
        _LOGGER.info("  no scored quant rows to pair yet")
        return
    for granularity, stats in sorted(
        report.pairing.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)
    ):
        _LOGGER.info(
            "  @%s: premise held on %d/%d re-run inputs -- quant %d / heuristic %d "
            "/ even %d (guard fired on re-run: %d, excluded; unparseable: %d)",
            _granularity_label(granularity),
            stats.held,
            stats.rerun,
            stats.quant_wins,
            stats.heuristic_wins,
            stats.even,
            sum(stats.guard_fired.values()),
            stats.unparseable,
        )
        for reason, count in stats.guard_fired.most_common():
            _LOGGER.info("    guard fired on re-run x%d: %s", count, reason)


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        # A read-only report must not connect-and-create an empty DB at
        # a mistyped path and then report "ledger empty."
        _LOGGER.error("advise DB not found: %s", db_path)
        return 2

    heuristic_spec: HeuristicSpec | None = None
    skip_reason: str | None = None
    try:
        if args.heuristic_file is not None:
            # Direct override for analyzing another deployment's corpus
            # (e.g. the desktop reporting on a NAS copy, where the local
            # settings don't run the cascade at all).
            heuristic_spec = load_heuristic_spec(Path(args.heuristic_file))
        else:
            config = load_resolved_config(config_path=args.config, profile_name=args.profile)
            heuristic_file = config.advisor.heuristic_file
            if heuristic_file is None:
                skip_reason = (
                    "advisor.heuristic_file is not configured "
                    "(pass --heuristic-file to pair against a specific spec)"
                )
            else:
                heuristic_spec = load_heuristic_spec(Path(heuristic_file))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        skip_reason = f"config/spec load failed ({exc})"

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        report = await build_report(
            storage,
            evaluator_version=args.evaluator_version,
            heuristic_spec=heuristic_spec,
            pairing_skip_reason=skip_reason,
        )
    finally:
        await storage.close()
    _render(report, args.evaluator_version)
    return 0


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--db",
        default=str(_DEFAULT_ADVISE_DB),
        help="Advise DB holding the outcome ledger.",
    )
    parser.add_argument(
        "--evaluator-version",
        type=int,
        default=EVALUATOR_VERSION,
        help="Which evaluator version's rows to report (default: current).",
    )
    parser.add_argument(
        "--heuristic-file",
        default=None,
        help=(
            "Heuristic spec YAML for the pairing (default: the settings' "
            "advisor.heuristic_file; e.g. config/heuristic/quant.yml — the "
            "shipped cascade guard spec)."
        ),
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

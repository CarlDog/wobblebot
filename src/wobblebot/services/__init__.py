"""
Services layer - Orchestration and coordination logic.

This layer contains services that orchestrate between domain logic and adapters,
manage application lifecycle, scheduling, and cross-cutting concerns.
"""

from wobblebot.services.aggregators import (
    aggregate_voting,
    aggregate_weighted_confidence,
)
from wobblebot.services.auto_apply import (
    AppliedKey,
    AutoApplyResult,
    RejectedKey,
    evaluate_auto_apply,
)
from wobblebot.services.grid_engine import GridEngine, StepResult
from wobblebot.services.metrics import (
    CycleStats,
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)
from wobblebot.services.settings_rewriter import (
    SettingsRewriteError,
    apply_grid_overrides,
)
from wobblebot.services.simulator import SimulationResult, run_buy_dip_sell_rebound_cycle
from wobblebot.services.summary_builder import SummaryBuilder

__all__ = [
    "AppliedKey",
    "AutoApplyResult",
    "CycleStats",
    "GridEngine",
    "RejectedKey",
    "SettingsRewriteError",
    "SimulationResult",
    "StepResult",
    "SummaryBuilder",
    "aggregate_voting",
    "aggregate_weighted_confidence",
    "apply_grid_overrides",
    "compute_cycle_stats",
    "compute_flatness",
    "compute_max_drawdown",
    "compute_volatility",
    "evaluate_auto_apply",
    "run_buy_dip_sell_rebound_cycle",
]

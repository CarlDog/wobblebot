"""
Services layer - Orchestration and coordination logic.

This layer contains services that orchestrate between domain logic and adapters,
manage application lifecycle, scheduling, and cross-cutting concerns.
"""

from wobblebot.services.aggregators import (
    aggregate_voting,
    aggregate_weighted_confidence,
)
from wobblebot.services.grid_engine import GridEngine, StepResult
from wobblebot.services.metrics import (
    CycleStats,
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)
from wobblebot.services.simulator import SimulationResult, run_buy_dip_sell_rebound_cycle
from wobblebot.services.summary_builder import SummaryBuilder

__all__ = [
    "CycleStats",
    "GridEngine",
    "SimulationResult",
    "StepResult",
    "SummaryBuilder",
    "aggregate_voting",
    "aggregate_weighted_confidence",
    "compute_cycle_stats",
    "compute_flatness",
    "compute_max_drawdown",
    "compute_volatility",
    "run_buy_dip_sell_rebound_cycle",
]

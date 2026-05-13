"""
Services layer - Orchestration and coordination logic.

This layer contains services that orchestrate between domain logic and adapters,
manage application lifecycle, scheduling, and cross-cutting concerns.
"""

from wobblebot.services.simulator import SimulationResult, run_buy_dip_sell_rebound_cycle

__all__ = ["run_buy_dip_sell_rebound_cycle", "SimulationResult"]

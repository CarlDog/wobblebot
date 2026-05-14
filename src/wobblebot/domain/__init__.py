"""
Domain layer - Core business logic and models.

This layer contains pure domain logic with zero external I/O dependencies.
All domain models, value objects, and deterministic business rules live here.
"""

from wobblebot.domain.exceptions import (
    DailySpendCapExceeded,
    ExposureLimitExceeded,
    InsufficientBalance,
    InvalidAmount,
    InvalidGridConfiguration,
    InvalidOrderState,
    InvalidPriceRange,
    WobbleBotDomainError,
)
from wobblebot.domain.grid import (
    GridLevel,
    GridSlot,
    compute_grid_levels,
    grid_spacing,
    is_offside,
    next_counter_action,
)
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp

__all__ = [
    # Exceptions
    "WobbleBotDomainError",
    "ExposureLimitExceeded",
    "DailySpendCapExceeded",
    "InvalidOrderState",
    "InvalidGridConfiguration",
    "InsufficientBalance",
    "InvalidPriceRange",
    "InvalidAmount",
    # Models
    "Order",
    "Trade",
    "Balance",
    # Note: Position deferred to Phase 3+ (see ADR-005)
    # Value Objects
    "Symbol",
    "Price",
    "Amount",
    "OrderSide",
    "Timestamp",
    # Grid math
    "GridLevel",
    "GridSlot",
    "compute_grid_levels",
    "grid_spacing",
    "is_offside",
    "next_counter_action",
]

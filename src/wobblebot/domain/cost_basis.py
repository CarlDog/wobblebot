"""Average cost-basis replay and sell-guard assessment (ADR-032).

Pure functions only — no config/services/adapter imports, per the
hexagonal layering rule (``domain/`` stays pure). ``services/cost_basis.py``
wraps these with the storage read, per-symbol cache, and throttled
logging.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import OrderSide


@dataclass(frozen=True)
class CostBasis:
    """A symbol's replayed average-cost position.

    ``average_cost`` is ``None`` when ``quantity`` is zero — no tracked
    basis (fresh deployment, or a pre-existing bag with no recorded BUYs).
    """

    quantity: Decimal
    average_cost: Decimal | None


def replay_average_cost(trades: list[Trade]) -> CostBasis:
    """Replay a symbol's trade history into a running average-cost basis.

    BUYs capitalize their fee into basis (``cost + fee``, spread over the
    acquired quantity); SELLs reduce holdings at the current running
    average without changing it. Quantity clamps at zero: a SELL for more
    than the tracked quantity (inventory held before the tracked history
    began) reduces the tracked position to empty rather than going
    negative, so any *subsequent* BUY starts a fresh, sound basis instead
    of carrying a phantom debt forward.
    """
    quantity = Decimal("0")
    total_cost = Decimal("0")
    for trade in sorted(trades, key=lambda t: t.executed_at.dt):
        if trade.side is OrderSide.BUY:
            quantity += trade.amount.value
            total_cost += trade.cost + trade.fee
            continue
        if quantity <= 0:
            continue
        sell_qty = min(trade.amount.value, quantity)
        average = total_cost / quantity
        total_cost -= average * sell_qty
        quantity -= sell_qty
        if quantity <= 0:
            quantity = Decimal("0")
            total_cost = Decimal("0")
    if quantity <= 0:
        return CostBasis(quantity=Decimal("0"), average_cost=None)
    return CostBasis(quantity=quantity, average_cost=total_cost / quantity)


@dataclass(frozen=True)
class SellAssessment:
    """Result of assessing one proposed SELL against a cost basis."""

    allowed: bool
    reason: str | None = None
    average_cost: Decimal | None = None
    loss_percentage: Decimal | None = None


def assess_sell(
    proposed_price: Decimal,
    basis: CostBasis,
    *,
    max_loss_percentage: Decimal,
    maker_fee_rate: Decimal,
) -> SellAssessment:
    """Decide whether a proposed SELL clears the cost-basis guard.

    Unknown basis (no tracked quantity) always allows — a fresh
    deployment or a pre-existing bag with no recorded BUYs must never
    freeze the sell ladder (ADR-032 decision 3); ``reason="unknown_basis"``
    lets the caller log a once-per-symbol notice without re-deriving it.

    ``maker_fee_rate`` nets the fee off the proposed price before
    comparing to basis (the basis already capitalized the BUY's fee) —
    a SELL at exactly the average BUY price scores a small structural
    loss from the round-trip fee alone, not zero.
    """
    if basis.average_cost is None or basis.quantity <= 0:
        return SellAssessment(allowed=True, reason="unknown_basis")
    net_proceeds = proposed_price * (Decimal("1") - maker_fee_rate)
    loss_percentage = (basis.average_cost - net_proceeds) / basis.average_cost * Decimal("100")
    if loss_percentage > max_loss_percentage:
        return SellAssessment(
            allowed=False,
            reason="below_cost_basis",
            average_cost=basis.average_cost,
            loss_percentage=loss_percentage,
        )
    return SellAssessment(
        allowed=True,
        average_cost=basis.average_cost,
        loss_percentage=loss_percentage,
    )


__all__ = ["CostBasis", "SellAssessment", "replay_average_cost", "assess_sell"]

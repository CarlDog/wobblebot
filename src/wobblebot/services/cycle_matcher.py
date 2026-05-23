"""Match BUY→SELL fill pairs into completed grid cycles.

The grid engine places a counter-SELL after every BUY fill (per
ADR-006 decision 2) and a counter-BUY after every SELL fill. The
match between a fill and its counter isn't persisted relationally
— this module reconstructs cycles by FIFO-pairing trades.

Used by:

- The web UI's status card to render "Recent Cycles" (matched
  pairs with realized per-cycle PnL).
- The web UI's "Today's PnL" header line (sum of realized PnL
  across cycles whose SELL fired today UTC).

Algorithm (FIFO by time, constrained by side semantics):

  For each SELL fill (oldest first):
      Find the OLDEST unmatched BUY for the same symbol
          where BUY price is LOWER than the SELL price.
      If found: pair them as one completed cycle.
      If not found: SELL is an "orphan" — sold inventory that
          wasn't bought during the observed window (typical for
          the first cycle after enabling the bot against
          pre-existing BTC inventory).

A BUY without a matching SELL stays unmatched — that's an
"in-flight" cycle whose counter-SELL hasn't filled yet. In-flight
fills are deliberately NOT included in the returned cycles list
(they appear in the Recent Fills feed instead).

Edge cases the matcher handles:

- Re-anchored grids: spacing changes between sessions don't break
  the matcher because we use observed prices, not the grid's
  configured spacing.
- Partial fills: each Trade row is treated as one matchable unit
  using its executed amount.
- Multiple symbols: pairing is scoped per-symbol; cross-symbol
  fills never match.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp


@dataclass(frozen=True)
class RecentCycle:  # pylint: disable=too-many-instance-attributes
    """One completed BUY→SELL pair with realized PnL.

    All amounts are in the SELL's quote currency (USD for BTC/USD).
    ``amount`` is the matched base-currency volume (the SELL's
    executed amount; assumes the BUY contributed at least this
    much — true under the grid engine's counter-sized-to-filled
    rule per ADR-006 decision 2).
    """

    symbol: Symbol
    buy_executed_at: Timestamp
    sell_executed_at: Timestamp
    buy_price: Price
    sell_price: Price
    amount: Amount
    buy_fee: Decimal
    sell_fee: Decimal
    net_pnl: Decimal
    """Net realized PnL for the cycle.

    Computed as ``(sell_price − buy_price) × amount − buy_fee − sell_fee``.
    Positive = profit; negative = loss. The grid is designed to
    always sell higher than it bought, so negative cycles are rare
    but possible (e.g., a cap-trip-and-restart interrupted the
    counter-placement and the operator manually closed at a loss).
    """


def match_cycles(trades: Sequence[Trade]) -> list[RecentCycle]:
    """FIFO-pair BUY→SELL trades into completed cycles.

    Trades may arrive in any order; the matcher sorts internally.
    Returned cycles are ordered newest-first (by sell_executed_at,
    matching the Recent Fills convention).

    Args:
        trades: Closed Trade rows from storage. Both BUY and SELL
            sides are needed; passing only one side returns an
            empty list.

    Returns:
        List of completed cycles. May be empty.
    """
    # Defensive sort — callers may pass arbitrary order.
    sorted_trades = sorted(trades, key=lambda t: t.executed_at.dt)

    # Per-symbol queues of unmatched BUYs, oldest first.
    pending_buys: dict[Symbol, list[Trade]] = {}
    cycles: list[RecentCycle] = []

    for trade in sorted_trades:
        if trade.side == "buy":
            pending_buys.setdefault(trade.symbol, []).append(trade)
            continue
        # It's a SELL. Find the oldest cheaper unmatched BUY for
        # this symbol.
        buys = pending_buys.get(trade.symbol, [])
        matched_buy: Trade | None = None
        for candidate in buys:
            if candidate.price.amount < trade.price.amount:
                matched_buy = candidate
                break
        if matched_buy is None:
            # Orphan SELL: no cheaper BUY in the observed window.
            # Most common cause is selling pre-existing inventory
            # that pre-dates the trades table. Don't synthesize a
            # cycle from data we don't have.
            continue
        buys.remove(matched_buy)
        # Use the SELL's executed amount as the cycle amount — the
        # grid engine sizes counter-orders to the filled amount
        # (ADR-006 decision 2), so this matches the cycle's true
        # base-currency exposure. The BUY may have been larger if
        # the operator pre-funded inventory; we don't claim that
        # excess as part of this cycle.
        cycle_amount = trade.amount.value
        gross_pnl = (trade.price.amount - matched_buy.price.amount) * cycle_amount
        net_pnl = gross_pnl - matched_buy.fee - trade.fee
        cycles.append(
            RecentCycle(
                symbol=trade.symbol,
                buy_executed_at=matched_buy.executed_at,
                sell_executed_at=trade.executed_at,
                buy_price=matched_buy.price,
                sell_price=trade.price,
                amount=trade.amount,
                buy_fee=matched_buy.fee,
                sell_fee=trade.fee,
                net_pnl=net_pnl,
            )
        )

    cycles.sort(key=lambda c: c.sell_executed_at.dt, reverse=True)
    return cycles


def today_realized_pnl(
    cycles: Sequence[RecentCycle],
    *,
    now: datetime | None = None,
) -> Decimal:
    """Sum net_pnl across cycles whose SELL fired today (UTC).

    "Today" = the calendar date in UTC. A cycle whose BUY fired
    yesterday but whose SELL fired today counts toward today —
    PnL is realized at the SELL.

    Args:
        cycles: Completed cycles from ``match_cycles``.
        now: Override for the current time (testing seam). Defaults
            to ``datetime.now(UTC)``.

    Returns:
        Sum of net_pnl, or ``Decimal(0)`` when no cycles match.
    """
    reference = now if now is not None else datetime.now(UTC)
    today = reference.astimezone(UTC).date()
    total = Decimal(0)
    for cycle in cycles:
        if cycle.sell_executed_at.dt.astimezone(UTC).date() == today:
            total += cycle.net_pnl
    return total


__all__ = ("RecentCycle", "match_cycles", "today_realized_pnl")

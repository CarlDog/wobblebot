"""Shared exposure + daily-spend math for the four safety caps.

Extracted 2026-08-10 so the **risk advisor sees the same numbers the
engine enforces**. `GridEngine._check_safety` computed these inline;
the SummaryBuilder now needs them too (`risk.md` promises the model
"current open exposure vs the configured caps … and daily spend so far
vs the daily cap"). Two independent implementations of the same rule
would drift, and the drift would be silent — the advisor would reason
about headroom the engine doesn't agree exists.

**The subtle rule lives here, not at the call sites.** Daily spend is
not a plain sum over today's BUYs: canceled and expired orders never
moved money, so counting them means every cancellation permanently eats
the day's headroom. That was a real incident (2026-05-22, soak Day 5) —
re-anchors plus an auto-re-layout stuffed 11 canceled BUYs into the
counter and blocked legitimate placements at $110 against a $100 cap.
Anything computing "spend" must go through :func:`daily_spend_usd`.

Every function here is a **read-only projection of storage**. They take
a ``StoragePort`` rather than an in-memory tally deliberately: storage
is the shared truth across the engine, the advisor daemon, and the web
tier, none of which share process state.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, time
from decimal import Decimal

from wobblebot.domain.models import Order, OrderSide
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.storage import StoragePort

# Order statuses that represent committed funds for the
# max_daily_spend_usd cap: "open" = funds locked at the exchange,
# "pending" = in-flight to the exchange, "closed" = filled. canceled +
# expired never moved money. See the module docstring for the incident
# that produced this rule.
SPEND_COMMITTED_STATUSES: frozenset[str] = frozenset({"open", "pending", "closed"})


def notional_usd(orders: Iterable[Order]) -> Decimal:
    """Sum ``price x amount`` over ``orders``.

    Equals the configured ``order_size_usd`` modulo Decimal-division
    rounding, which is acceptable: cap thresholds are operator-set in
    whole dollars, far above any rounding artifact.
    """
    return sum((o.price.amount * o.amount.value for o in orders), Decimal("0"))


async def coin_exposure_usd(storage: StoragePort, symbol: Symbol) -> Decimal:
    """Open-order notional for one symbol (the per-coin exposure cap)."""
    return notional_usd(await storage.get_open_orders(symbol=symbol))


async def total_exposure_usd(storage: StoragePort) -> Decimal:
    """Open-order notional across every symbol (the total exposure cap)."""
    return notional_usd(await storage.get_open_orders())


async def daily_spend_usd(storage: StoragePort, *, now: datetime | None = None) -> Decimal:
    """Committed BUY notional since midnight UTC (the daily spend cap).

    Args:
        storage: Where orders live.
        now: Override for "today", for tests. Defaults to ``datetime.now(UTC)``.

    Only BUYs count — a SELL returns capital rather than committing it —
    and only those in :data:`SPEND_COMMITTED_STATUSES`.
    """
    moment = now if now is not None else datetime.now(UTC)
    today_start = datetime.combine(moment.date(), time.min, tzinfo=UTC)
    todays_buys = await storage.get_orders(side=OrderSide.BUY.value, created_after=today_start)
    committed = [o for o in todays_buys if o.status in SPEND_COMMITTED_STATUSES]
    return notional_usd(committed)

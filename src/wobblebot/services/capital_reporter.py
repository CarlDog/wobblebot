"""Capital Reporter — the read-only half of ADR-040 (2026-08-22).

Three deterministic checks, one per failure class the 2026-08-22
session surfaced. Advisory only: this module computes findings and
returns them. It writes nothing, changes no config, and places no
orders.

**Why it computes conditions instead of reading refusal reasons.**
``GridEngine._check_safety`` runs BEFORE the order reaches Kraken, so
whichever gate trips first is the only one you see. Observed live the
same day: SOL's BUY at 86.0223 was refused by the exchange for
``ordermin`` in the morning and by ``max_daily_spend_usd`` that
evening — same symbol, same price, same two underlying defects, and a
log-scraping checker would have produced a different diagnosis
depending on the hour. Each check here is derived from config +
exchange metadata + the ledger, never from what the engine happened to
report.

The three checks, and the finding each one exists to catch:

1. :func:`check_entry_viability` — does ``order_size_usd`` buy at
   least ``ordermin`` at every grid level? SOL fails: $5 at ~$86-103
   yields ~0.058 SOL against a 0.06 minimum, so it cannot ENTER.
2. :func:`check_exit_viability` — is the HELD position itself at least
   ``ordermin``? SOL fails here too (0.05876515 held, 98% of the
   minimum), so it cannot EXIT either. This check would not have been
   specified from first principles; SOL surfaced it.
3. :func:`compute_cap_honesty` — is ``max_daily_spend_usd`` consumption
   tracking NET capital deployed? XRP fails: on 2026-08-22 the account
   deployed $113.88, got $108.32 back (95% recycled, $5.57 net out),
   and the cap read ~$118 consumed — roughly 21x the capital that
   moved. ``daily_spend_usd`` counts ``closed`` BUYs, which never
   release until midnight UTC, so a grid that cycles WELL locks itself
   out sooner.

Nothing here interprets a finding as "the cap is wrong". Check 3
reports the gap; whether that gap is a defect or intended conservatism
is an operator judgement, and ADR-040 records that narrowing it needs
its own ADR.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Literal

from wobblebot.domain.grid import compute_grid_levels
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import OrderSide, PairLimits, Symbol

HeldSource = Literal["exchange", "replay"]


@dataclass(frozen=True)
class EntryViability:
    """Can the configured order size place at every grid level?"""

    symbol: Symbol
    order_size_usd: Decimal
    ordermin: Decimal
    costmin: Decimal
    blocked_prices: tuple[Decimal, ...]
    total_levels: int
    # Smallest order_size_usd that clears BOTH minimums at EVERY level.
    # None when nothing is blocked.
    required_order_size_usd: Decimal | None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_prices)

    @property
    def fully_blocked(self) -> bool:
        """No level can place — the symbol cannot enter the market at all."""
        return self.total_levels > 0 and len(self.blocked_prices) == self.total_levels


@dataclass(frozen=True)
class ExitViability:
    """Can the held position be sold as a single order?

    A position below ``ordermin`` is unsellable in one order regardless
    of order sizing — the asset is stranded until the position grows.
    """

    symbol: Symbol
    held_quantity: Decimal
    ordermin: Decimal
    source: HeldSource

    @property
    def blocked(self) -> bool:
        # A zero position is not a finding: nothing to exit.
        return Decimal(0) < self.held_quantity < self.ordermin


@dataclass(frozen=True)
class CapHonesty:
    """Daily-spend cap consumption vs capital that actually left."""

    charged_usd: Decimal  # what daily_spend_usd() reports
    cap_usd: Decimal
    gross_bought_usd: Decimal
    gross_sold_usd: Decimal

    @property
    def net_deployed_usd(self) -> Decimal:
        """Capital that actually left the account today. May be negative
        (a net-selling day returns more than it took)."""
        return self.gross_bought_usd - self.gross_sold_usd

    @property
    def recycled_usd(self) -> Decimal:
        """Capital deployed and returned within the day."""
        return min(self.gross_bought_usd, self.gross_sold_usd)

    @property
    def consumed_fraction(self) -> Decimal | None:
        if self.cap_usd <= 0:
            return None
        return self.charged_usd / self.cap_usd

    @property
    def overstatement_ratio(self) -> Decimal | None:
        """How many times the charge exceeds net capital deployed.

        ``None`` when net deployment is zero or negative — the ratio is
        undefined there, and reporting a huge number for a net-selling
        day would misread as a worse defect rather than a different one.
        Callers should treat ``net_deployed_usd <= 0`` with a nonzero
        ``charged_usd`` as its own (stronger) finding.
        """
        net = self.net_deployed_usd
        if net <= 0:
            return None
        return self.charged_usd / net


def check_entry_viability(  # pylint: disable=too-many-arguments
    # The grid's shape is six independent numbers plus its limits.
    # Bundling them into a DTO purely to satisfy the argument count
    # would invent a type with exactly one construction site. They
    # are keyword-only, so call sites stay self-documenting.
    symbol: Symbol,
    *,
    order_size_usd: Decimal,
    reference_price: Decimal,
    spacing_percentage: Decimal,
    levels_above: int,
    levels_below: int,
    limits: PairLimits,
) -> EntryViability:
    """Which grid levels would Kraken refuse for ``ordermin``/``costmin``?

    Levels come from the engine's own :func:`compute_grid_levels`, not a
    reimplementation — a Reporter that computed a different ladder than
    the engine places would report on a grid that does not exist.
    """
    levels = compute_grid_levels(
        reference_price=reference_price,
        spacing_percentage=spacing_percentage,
        levels_above=levels_above,
        levels_below=levels_below,
    )
    blocked: list[Decimal] = []
    for level in levels:
        if level.price <= 0:
            continue
        volume = order_size_usd / level.price
        if volume < limits.ordermin or order_size_usd < limits.costmin:
            blocked.append(level.price)

    required: Decimal | None = None
    if blocked:
        # The HIGHEST level is the binding one: volume = size / price, so
        # the largest price yields the smallest volume. Clearing that one
        # clears them all. costmin is a floor on the USD side and applies
        # at every level equally.
        highest = max(level.price for level in levels)
        required = max(limits.ordermin * highest, limits.costmin)

    return EntryViability(
        symbol=symbol,
        order_size_usd=order_size_usd,
        ordermin=limits.ordermin,
        costmin=limits.costmin,
        blocked_prices=tuple(blocked),
        total_levels=len(levels),
        required_order_size_usd=required,
    )


def check_exit_viability(
    symbol: Symbol,
    *,
    held_quantity: Decimal,
    limits: PairLimits,
    source: HeldSource,
) -> ExitViability:
    """Is the held position large enough to sell in one order?

    ``source`` records where the quantity came from. The exchange
    balance is authoritative — it is what ``ordermin`` is judged
    against — and the replayed ledger is the credential-free fallback.
    The 2026-08-22 silent-fill-loss incident is why the distinction is
    carried rather than assumed identical.
    """
    return ExitViability(
        symbol=symbol,
        held_quantity=held_quantity,
        ordermin=limits.ordermin,
        source=source,
    )


def utc_day_start(now: datetime | None = None) -> datetime:
    """Midnight UTC of ``now``'s date — the daily cap's reset boundary.

    Mirrors ``services.exposure.daily_spend_usd``'s own window so the
    Reporter measures the same day the cap enforces.
    """
    moment = now if now is not None else datetime.now(UTC)
    return datetime.combine(moment.date(), time.min, tzinfo=UTC)


def compute_cap_honesty(
    trades: Iterable[Trade],
    *,
    charged_usd: Decimal,
    cap_usd: Decimal,
    now: datetime | None = None,
) -> CapHonesty:
    """Gross/net capital flow for the current UTC day.

    ``charged_usd`` is what ``services.exposure.daily_spend_usd``
    reports — passed in rather than recomputed so the Reporter can
    never disagree with the cap the engine actually enforces.

    Uses ``Trade.cost`` (the executed notional) rather than order
    notional: the question is what capital MOVED, and an order that
    never filled moved none.
    """
    day_start = utc_day_start(now)
    bought = Decimal(0)
    sold = Decimal(0)
    for trade in trades:
        if trade.executed_at.dt < day_start:
            continue
        if trade.side is OrderSide.BUY:
            bought += trade.cost
        else:
            sold += trade.cost
    return CapHonesty(
        charged_usd=charged_usd,
        cap_usd=cap_usd,
        gross_bought_usd=bought,
        gross_sold_usd=sold,
    )


@dataclass(frozen=True)
class CapitalReport:
    """Everything one Reporter cycle found. Pure data."""

    entry: tuple[EntryViability, ...]
    exits: tuple[ExitViability, ...]
    cap: CapHonesty | None

    @property
    def blocked_entries(self) -> tuple[EntryViability, ...]:
        return tuple(e for e in self.entry if e.blocked)

    @property
    def blocked_exits(self) -> tuple[ExitViability, ...]:
        return tuple(x for x in self.exits if x.blocked)

    @property
    def has_findings(self) -> bool:
        return bool(self.blocked_entries or self.blocked_exits)


def summarize(report: CapitalReport) -> Sequence[str]:
    """Operator-readable finding lines, most actionable first.

    Returned as lines rather than one blob so the caller can log them
    individually and put the same text in a notification without the
    two drifting apart.
    """
    lines: list[str] = []
    for item in report.blocked_entries:
        scope = "every level" if item.fully_blocked else f"{len(item.blocked_prices)} level(s)"
        required = item.required_order_size_usd
        lines.append(
            f"{item.symbol}: order_size_usd {item.order_size_usd} cannot place at {scope} "
            f"(ordermin {item.ordermin}, costmin {item.costmin}); "
            f"needs >= {required} to clear all levels"
        )
    for held in report.blocked_exits:
        lines.append(
            f"{held.symbol}: held {held.held_quantity} is below ordermin {held.ordermin} "
            f"({held.source} balance) — the position cannot be sold as a single order"
        )
    cap = report.cap
    if cap is not None:
        ratio = cap.overstatement_ratio
        if ratio is not None and ratio >= 2:
            lines.append(
                f"daily spend cap charged {cap.charged_usd} of {cap.cap_usd} against "
                f"{cap.net_deployed_usd} net capital deployed "
                f"({ratio:.1f}x; {cap.recycled_usd} recycled within the day)"
            )
        # Reads as: net deployment is at or below zero, which is below
        # what the cap was charged -- i.e. every charged dollar came
        # back and the cap is still holding them against us.
        elif cap.net_deployed_usd <= 0 < cap.charged_usd:
            lines.append(
                f"daily spend cap charged {cap.charged_usd} of {cap.cap_usd} on a "
                f"net-flat-or-selling day (net {cap.net_deployed_usd}); "
                "the cap is consumed entirely by completed round-trips"
            )
    return lines

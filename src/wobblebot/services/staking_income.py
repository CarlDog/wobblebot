"""Income the trade ledger cannot see (ADR-040 follow-up, 2026-08-22).

Discovered while investigating why SOL's replayed quantity disagreed
with Kraken's balance: SOL, ETH and ADA each exceeded their replayed
position by *exactly* their net staking income, while unstaked BTC, XRP
and DOGE matched to the digit. Nothing was lost -- the account was
earning money that no accounting surface in the project recorded.

**This module deliberately does not touch cost basis.** A staking
reward is a zero-cost acquisition, so counting it would leave
``quantity x average_cost`` unchanged (more units, same dollars) while
diluting the per-unit average the ADR-032 sell guard reasons about.
ADR-039's inventory cap measures dollars at risk and is already correct
without it. Income is reported ALONGSIDE realized P&L, never folded
into basis.

**Why classification is a denylist, not an allowlist.** ``trade`` rows
are already explained by the trades table, and transfers/deposits/
withdrawals move capital without earning it. Everything else counts as
income. An allowlist of known reward types would silently drop the next
reward type the exchange invents -- which is the failure this module
exists to end, not repeat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from wobblebot.domain.models import LedgerEntry

# Ledger types that are NOT income. Everything else is.
#
# - trade: already accounted for in the trades table; counting it here
#   would double-count every fill.
# - deposit / withdrawal / transfer: capital moving in or out. The
#   operator funding the account is not the account earning.
# - adjustment / rollover / settled / margin: corrections and
#   derivatives bookkeeping, not earnings.
NON_INCOME_TYPES: frozenset[str] = frozenset(
    {
        "trade",
        "deposit",
        "withdrawal",
        "transfer",
        "adjustment",
        "rollover",
        "settled",
        "margin",
    }
)


@dataclass(frozen=True)
class AssetIncome:
    """Non-trade income earned in one asset, in that asset's units."""

    asset: str
    gross: Decimal
    fee: Decimal
    entry_count: int
    entry_types: tuple[str, ...]

    @property
    def net(self) -> Decimal:
        """What actually reached the balance.

        The 2026-08-22 reconciliation only closed once the fee was
        subtracted: Kraken bills staking at 30%, so summing ``gross``
        alone overstated income by nearly a third and made the balances
        look like they still disagreed.
        """
        return self.gross - self.fee

    @property
    def fee_fraction(self) -> Decimal | None:
        """Share of gross taken by the exchange. ``None`` when gross is 0."""
        if self.gross == 0:
            return None
        return self.fee / self.gross


def income_by_asset(entries: Iterable[LedgerEntry]) -> dict[str, AssetIncome]:
    """Aggregate non-trade income per asset, in base units.

    Only positive-amount entries count. A negative non-trade entry is
    an outflow (an unstaking penalty, a clawback) and must not be
    silently netted into an income figure -- that would report a loss
    as smaller earnings.
    """
    gross: dict[str, Decimal] = {}
    fees: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    types: dict[str, set[str]] = {}
    for entry in entries:
        if entry.entry_type in NON_INCOME_TYPES or entry.amount <= 0:
            continue
        gross[entry.asset] = gross.get(entry.asset, Decimal(0)) + entry.amount
        fees[entry.asset] = fees.get(entry.asset, Decimal(0)) + entry.fee
        counts[entry.asset] = counts.get(entry.asset, 0) + 1
        types.setdefault(entry.asset, set()).add(entry.entry_type)
    return {
        asset: AssetIncome(
            asset=asset,
            gross=amount,
            fee=fees[asset],
            entry_count=counts[asset],
            entry_types=tuple(sorted(types[asset])),
        )
        for asset, amount in gross.items()
    }


def value_income_usd(
    income: dict[str, AssetIncome], prices: dict[str, Decimal]
) -> tuple[Decimal, tuple[str, ...]]:
    """Value net income at the supplied per-asset prices.

    Returns ``(total_usd, unpriced_assets)``. An asset with no price is
    EXCLUDED and named, never valued at zero: a silently-dropped asset
    reads as "no income" and would hide exactly what this is measuring.

    Valuation is at the CALLER's prices -- normally current market, not
    fair value at receipt. That answers "what are the rewards worth
    now", which is the operator-facing question. Per-receipt valuation
    would need a historical price lookup per entry and is a separate
    concern (it matters for tax, which this project does not do).
    """
    total = Decimal(0)
    unpriced: list[str] = []
    for asset, item in income.items():
        price = prices.get(asset)
        if price is None:
            unpriced.append(asset)
            continue
        total += item.net * price
    return total, tuple(sorted(unpriced))

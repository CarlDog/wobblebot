"""Harvester decision logic (Stage 4.1).

Pure functions: given a Kraken USD balance, the operator's
``HarvesterConfig`` thresholds, and a rolling 24h withdrawal total,
return a ``TransferProposal`` or ``None``.

No I/O. No Kraken calls. No daemon. The exchange-side mechanics
(reading balances via ``ExchangePort``, executing withdrawals,
persisting transfer history) land in Stage 4.2+ adapters that
consume this module's decision output.

**The three bands** the threshold ordering carves out:

::

      0  ─────────── min ─────── topup ─────── surplus ─── ∞
         ↑ DEFICIT   ↑           ↑             ↑ SURPLUS
                     │           │
                     │ TOP-UP    │ HOLD
                     │ BAND      │ BAND

- **Deficit** (balance < min): the engine is below the operator's
  defined liquidity floor. We do NOT propose anything here — the
  operator's manual judgment is wanted. If we auto-deposited the
  engine could get topped up while down on its luck and just bleed
  more. Phase 4 doesn't try to be smarter than the operator about
  this edge.
- **Top-up band** (min ≤ balance < topup): below the low-water
  mark but still funded. Propose a bank→Kraken deposit bringing
  balance up to the *midpoint* of the hold band (between topup and
  surplus). Conservative; avoids ping-ponging.
- **Hold band** (topup ≤ balance ≤ surplus): everything is fine.
  Return None.
- **Surplus** (balance > surplus): excess. Propose a Kraken→bank
  withdrawal of the excess down to the same midpoint of the hold
  band.

**Day-cap interaction:** ``today_total_withdrawn_usd`` is passed in
by the caller (which is responsible for computing the rolling 24h
sum from persisted withdrawal history). If the proposed
exchange→bank amount would push today's total over
``max_withdrawal_per_day_usd``, we propose the largest amount that
fits within the remaining cap. If the cap is fully exhausted, return
None — operator must wait for the window to roll.

The day-cap doesn't apply to bank→Kraken deposits — those are
deposits, not withdrawals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from wobblebot.config.harvester import HarvesterConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.harvester import TransferProposal, TransferResult


class _TransferHistoryReader(Protocol):
    """Subset of ``StoragePort`` ``compute_today_total_withdrawn_usd``
    needs. Keeps this module's import surface narrow (avoids dragging
    the full storage port into the pure-domain layer)."""

    async def get_transfer_results(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        since: datetime | None = None,
        status: str | None = None,
        asset: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
    ) -> list[TransferResult]: ...


async def compute_today_total_withdrawn_usd(
    storage: _TransferHistoryReader,
    *,
    asset: str = "USD",
    now: datetime | None = None,
    window: timedelta = timedelta(hours=24),
) -> Decimal:
    """Sum exchange→bank withdrawals over a rolling 24h window.

    Stage 4.4b's day-cap input: feeds ``today_total_withdrawn_usd``
    into ``propose_transfer()``. Pre-4.4b the daemon always passed
    ``Decimal("0")`` (no history); now it reads the actual recent
    withdrawals.

    Definition of "today":
    - **Rolling 24h** from ``now`` (default ``datetime.now(UTC)``),
      not calendar-day-based. A withdrawal at 23:55 UTC counts
      against the cap for the next ~24h, not just until the next
      midnight. Operator-friendly: if the operator submits at
      midnight, they don't get a fresh cap five minutes later.
    - Includes ``status in ("pending", "completed")``. Failed
      withdrawals don't count — they didn't actually move money.

    Args:
        storage: Anything with a ``get_transfer_results`` method
          matching the StoragePort signature.
        asset: Asset to sum (defaults to "USD" — Stage 4.4's only
          supported asset).
        now: Override for tests; defaults to wall-clock UTC.
        window: Rolling window size. 24h is the only sensible value
          for the day-cap, but parameterized for future flexibility.

    Returns:
        Sum of executed_amount for matching results, or Decimal('0')
        when the history is empty.
    """
    cutoff = (now or datetime.now(UTC)) - window
    # Filter at the SQL level (since/asset/direction) so we don't
    # haul every transfer_result row into Python on a long-running
    # account.
    results = await storage.get_transfer_results(
        since=cutoff,
        asset=asset,
        direction="exchange_to_bank",
    )
    total = Decimal("0")
    for r in results:
        if r.status in ("pending", "completed"):
            total += r.executed_amount
    return total


def propose_transfer(
    *,
    balance_usd: Decimal,
    config: HarvesterConfig,
    today_total_withdrawn_usd: Decimal = Decimal("0"),
) -> TransferProposal | None:
    """Decide whether the Harvester should propose a transfer.

    Args:
        balance_usd: Current Kraken USD balance (the Phase 4.1 surface
            is USD-only; per-asset coverage deferred).
        config: ``HarvesterConfig`` with the four thresholds. Ordering
            (``min < topup < surplus``) is validated at config-load.
        today_total_withdrawn_usd: Cumulative exchange→bank withdrawals
            over the rolling 24h window. Defaults to ``0`` for callers
            that haven't wired the history query yet.

    Returns:
        A ``TransferProposal`` when action is warranted, ``None``
        otherwise (in the hold band, in deficit, or day-cap exhausted).

    The proposal carries a fresh UUID, a direction (``exchange_to_bank``
    or ``bank_to_exchange``), the proposed amount, and a one-line
    rationale that operators can read in logs.
    """
    if balance_usd < config.min_exchange_liquidity_usd:
        # Deficit — operator-only territory; we do nothing.
        return None

    if balance_usd > config.surplus_threshold_usd:
        return _propose_withdrawal(balance_usd, config, today_total_withdrawn_usd)

    if balance_usd < config.topup_threshold_usd:
        return _propose_topup(balance_usd, config)

    # Hold band — nothing to do.
    return None


def _propose_withdrawal(
    balance_usd: Decimal,
    config: HarvesterConfig,
    today_total_withdrawn_usd: Decimal,
) -> TransferProposal | None:
    """Propose an exchange→bank scrape from above the surplus threshold."""
    hold_midpoint = _hold_band_midpoint(config)
    desired_amount = balance_usd - hold_midpoint

    remaining_cap = config.max_withdrawal_per_day_usd - today_total_withdrawn_usd
    if remaining_cap <= Decimal("0"):
        # Day-cap exhausted; operator must wait for the window to roll.
        return None
    actual_amount = min(desired_amount, remaining_cap)
    if actual_amount <= Decimal("0"):
        return None

    target_balance = balance_usd - actual_amount
    rationale = (
        f"balance ${balance_usd} above surplus_threshold ${config.surplus_threshold_usd}; "
        f"scrape ${actual_amount} to bank (target post-scrape balance ${target_balance})"
    )
    if actual_amount < desired_amount:
        rationale += (
            f"; constrained by max_withdrawal_per_day_usd "
            f"(today's total ${today_total_withdrawn_usd} + ${actual_amount} "
            f"= cap ${config.max_withdrawal_per_day_usd})"
        )

    return TransferProposal(
        proposal_id=str(uuid4()),
        direction="exchange_to_bank",
        asset="USD",
        amount=actual_amount,
        rationale=rationale,
        current_exchange_balance=balance_usd,
        target_exchange_balance=target_balance,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _propose_topup(
    balance_usd: Decimal,
    config: HarvesterConfig,
) -> TransferProposal:
    """Propose a bank→exchange deposit from inside the top-up band."""
    hold_midpoint = _hold_band_midpoint(config)
    amount = hold_midpoint - balance_usd
    rationale = (
        f"balance ${balance_usd} between min_exchange_liquidity "
        f"${config.min_exchange_liquidity_usd} and topup_threshold "
        f"${config.topup_threshold_usd}; deposit ${amount} from bank "
        f"(target post-deposit balance ${hold_midpoint})"
    )
    return TransferProposal(
        proposal_id=str(uuid4()),
        direction="bank_to_exchange",
        asset="USD",
        amount=amount,
        rationale=rationale,
        current_exchange_balance=balance_usd,
        target_exchange_balance=hold_midpoint,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _hold_band_midpoint(config: HarvesterConfig) -> Decimal:
    """Midpoint of (topup, surplus) — the post-transfer balance target.

    Conservative choice that avoids ping-ponging: after a scrape we
    don't land right at the surplus threshold (where the next tick
    would maybe scrape again), and after a top-up we don't land right
    at topup (where the next tick would maybe top up again). The
    midpoint gives both sides headroom.
    """
    return (config.topup_threshold_usd + config.surplus_threshold_usd) / Decimal("2")

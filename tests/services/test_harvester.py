"""Unit tests for the Stage 4.1 Harvester decision logic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wobblebot.config.harvester import HarvesterConfig
from wobblebot.ports.harvester import TransferProposal
from wobblebot.services.harvester import propose_transfer

pytestmark = pytest.mark.unit


def _config(
    *,
    enabled: bool = True,
    min_liq: str = "200",
    topup: str = "250",
    surplus: str = "500",
    day_cap: str = "1000",
) -> HarvesterConfig:
    return HarvesterConfig(
        enabled=enabled,
        min_exchange_liquidity_usd=Decimal(min_liq),
        topup_threshold_usd=Decimal(topup),
        surplus_threshold_usd=Decimal(surplus),
        max_withdrawal_per_day_usd=Decimal(day_cap),
    )


# ----- Hold band (no-op) -----


class TestHoldBand:
    def test_exactly_at_topup_returns_none(self) -> None:
        """topup ≤ balance ≤ surplus is the hold band — no proposal."""
        assert propose_transfer(balance_usd=Decimal("250"), config=_config()) is None

    def test_exactly_at_surplus_returns_none(self) -> None:
        """Surplus is inclusive — proposal only when *above* surplus."""
        assert propose_transfer(balance_usd=Decimal("500"), config=_config()) is None

    def test_mid_hold_band_returns_none(self) -> None:
        assert propose_transfer(balance_usd=Decimal("375"), config=_config()) is None


# ----- Deficit (no-op, operator-only territory) -----


class TestDeficit:
    def test_below_min_returns_none(self) -> None:
        """Below the floor is operator-only territory — no auto-deposit."""
        assert propose_transfer(balance_usd=Decimal("100"), config=_config()) is None

    def test_exactly_at_min_proposes_topup(self) -> None:
        """At-floor is in the top-up band (min ≤ balance < topup).
        Floor is inclusive on the top-up side — the balance is still
        considered "above the floor.\" """
        result = propose_transfer(balance_usd=Decimal("200"), config=_config())
        assert result is not None
        assert result.direction == "bank_to_exchange"

    def test_just_below_min_returns_none(self) -> None:
        result = propose_transfer(balance_usd=Decimal("199.99"), config=_config())
        assert result is None


# ----- Top-up band (bank → exchange) -----


class TestTopupBand:
    def test_below_topup_proposes_deposit(self) -> None:
        """min ≤ balance < topup → bank→exchange proposal toward midpoint."""
        result = propose_transfer(balance_usd=Decimal("210"), config=_config())
        assert result is not None
        assert result.direction == "bank_to_exchange"
        assert result.asset == "USD"
        # Midpoint of (topup=250, surplus=500) = 375; deposit 375 - 210 = 165
        assert result.amount == Decimal("165")
        assert result.target_exchange_balance == Decimal("375")
        assert result.current_exchange_balance == Decimal("210")

    def test_topup_proposal_includes_rationale(self) -> None:
        result = propose_transfer(balance_usd=Decimal("210"), config=_config())
        assert result is not None
        assert "topup_threshold" in result.rationale
        assert "bank" in result.rationale

    def test_topup_does_not_consult_day_cap(self) -> None:
        """The day-cap is on withdrawals (outflows); top-ups (inflows)
        are unaffected."""
        result = propose_transfer(
            balance_usd=Decimal("210"),
            config=_config(day_cap="1"),  # tiny cap; doesn't constrain inflows
            today_total_withdrawn_usd=Decimal("0"),
        )
        assert result is not None
        assert result.direction == "bank_to_exchange"


# ----- Surplus (exchange → bank) -----


class TestSurplus:
    def test_above_surplus_proposes_withdrawal(self) -> None:
        """balance > surplus → exchange→bank proposal scraping to midpoint."""
        result = propose_transfer(balance_usd=Decimal("600"), config=_config())
        assert result is not None
        assert result.direction == "exchange_to_bank"
        assert result.asset == "USD"
        # Midpoint = 375; scrape 600 - 375 = 225
        assert result.amount == Decimal("225")
        assert result.target_exchange_balance == Decimal("375")
        assert result.current_exchange_balance == Decimal("600")

    def test_surplus_proposal_includes_rationale(self) -> None:
        result = propose_transfer(balance_usd=Decimal("600"), config=_config())
        assert result is not None
        assert "surplus_threshold" in result.rationale
        assert "bank" in result.rationale

    def test_target_post_scrape_below_surplus(self) -> None:
        """The midpoint must be below surplus so the next tick doesn't
        immediately scrape again — anti-ping-pong invariant."""
        cfg = _config(topup="250", surplus="500")
        result = propose_transfer(balance_usd=Decimal("600"), config=cfg)
        assert result is not None
        assert result.target_exchange_balance < cfg.surplus_threshold_usd

    def test_target_post_scrape_above_topup(self) -> None:
        """Conversely, the midpoint must be above topup so the next
        tick doesn't immediately propose a top-up."""
        cfg = _config(topup="250", surplus="500")
        result = propose_transfer(balance_usd=Decimal("600"), config=cfg)
        assert result is not None
        assert result.target_exchange_balance > cfg.topup_threshold_usd


# ----- Day-cap -----


class TestDayCap:
    def test_proposal_constrained_by_remaining_cap(self) -> None:
        """If the desired withdrawal exceeds the remaining day-cap,
        the proposal shrinks to the remaining cap."""
        result = propose_transfer(
            balance_usd=Decimal("1000"),
            config=_config(day_cap="100"),
            today_total_withdrawn_usd=Decimal("40"),
        )
        assert result is not None
        # Desired = 1000 - 375 = 625; remaining cap = 100 - 40 = 60
        assert result.amount == Decimal("60")
        # Rationale must flag the constraint so the operator sees why
        # the scrape is smaller than the surplus.
        assert "max_withdrawal_per_day_usd" in result.rationale

    def test_cap_fully_exhausted_returns_none(self) -> None:
        """If today's withdrawals already hit the cap, no further
        scrape — operator must wait for the rolling window to roll."""
        result = propose_transfer(
            balance_usd=Decimal("1000"),
            config=_config(day_cap="100"),
            today_total_withdrawn_usd=Decimal("100"),
        )
        assert result is None

    def test_cap_overflow_returns_none(self) -> None:
        """Defensive: today's total above the cap (shouldn't happen
        but the gate still refuses)."""
        result = propose_transfer(
            balance_usd=Decimal("1000"),
            config=_config(day_cap="100"),
            today_total_withdrawn_usd=Decimal("150"),
        )
        assert result is None

    def test_room_below_desired_below_cap_uses_full_desired(self) -> None:
        """If the desired amount fits comfortably under the remaining
        cap, the proposal isn't shrunk."""
        result = propose_transfer(
            balance_usd=Decimal("600"),
            config=_config(day_cap="10000"),
            today_total_withdrawn_usd=Decimal("100"),
        )
        assert result is not None
        # Desired = 600 - 375 = 225; well under remaining cap of 9900.
        assert result.amount == Decimal("225")
        # Rationale should NOT flag the constraint when it didn't bite.
        assert "constrained by max_withdrawal_per_day_usd" not in result.rationale


# ----- Schema invariants (config layer) -----


class TestConfigInvariants:
    def test_min_must_be_below_topup(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="topup_threshold"):
            HarvesterConfig(
                enabled=True,
                min_exchange_liquidity_usd=Decimal("300"),
                topup_threshold_usd=Decimal("250"),  # inverted
                surplus_threshold_usd=Decimal("500"),
                max_withdrawal_per_day_usd=Decimal("1000"),
            )

    def test_topup_must_be_below_surplus(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="surplus_threshold"):
            HarvesterConfig(
                enabled=True,
                min_exchange_liquidity_usd=Decimal("100"),
                topup_threshold_usd=Decimal("600"),  # inverted
                surplus_threshold_usd=Decimal("500"),
                max_withdrawal_per_day_usd=Decimal("1000"),
            )

    def test_all_thresholds_must_be_positive(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="min_exchange_liquidity_usd"):
            HarvesterConfig(
                enabled=True,
                min_exchange_liquidity_usd=Decimal("0"),
                topup_threshold_usd=Decimal("250"),
                surplus_threshold_usd=Decimal("500"),
                max_withdrawal_per_day_usd=Decimal("1000"),
            )

    def test_default_disabled(self) -> None:
        """The ``enabled`` flag must default to False — operator opts in
        before any code path that moves money."""
        cfg = HarvesterConfig(
            min_exchange_liquidity_usd=Decimal("200"),
            topup_threshold_usd=Decimal("250"),
            surplus_threshold_usd=Decimal("500"),
            max_withdrawal_per_day_usd=Decimal("1000"),
        )
        assert cfg.enabled is False


# ----- Proposal shape sanity -----


class TestProposalShape:
    def test_proposal_carries_fresh_uuid(self) -> None:
        """Two consecutive proposals for the same state get distinct IDs."""
        a = propose_transfer(balance_usd=Decimal("600"), config=_config())
        b = propose_transfer(balance_usd=Decimal("600"), config=_config())
        assert a is not None and b is not None
        assert a.proposal_id != b.proposal_id

    def test_topup_amount_strictly_positive(self) -> None:
        result = propose_transfer(balance_usd=Decimal("210"), config=_config())
        assert result is not None
        assert result.amount > Decimal("0")

    def test_returns_pydantic_model(self) -> None:
        result = propose_transfer(balance_usd=Decimal("600"), config=_config())
        assert isinstance(result, TransferProposal)

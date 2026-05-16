"""Unit tests for the Stage 4.1 Harvester decision logic + Stage 4.4b day-cap helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wobblebot.config.harvester import HarvesterConfig
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.services.harvester import compute_today_total_withdrawn_usd, propose_transfer

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


# ----- Day-cap history reader (Stage 4.4b) -----


class _StubHistory:
    """Just-enough storage-port-shaped object for the day-cap helper.

    Returns canned results when ``get_transfer_results`` is called.
    Captures the filter args so tests can assert the helper queried
    with the right window/asset/direction.
    """

    def __init__(self, results: list[TransferResult]) -> None:
        self.results = results
        self.last_since: object = None
        self.last_asset: object = None
        self.last_direction: object = None

    async def get_transfer_results(  # type: ignore[no-untyped-def]
        self,
        since=None,
        status=None,
        asset=None,
        direction=None,
        limit=None,
    ):
        self.last_since = since
        self.last_asset = asset
        self.last_direction = direction
        # The helper expects the storage adapter to apply since/asset/
        # direction filters at the SQL level. Stub follows the same
        # contract.
        out = []
        for r in self.results:
            if since is not None and r.timestamp.dt < since:
                continue
            if asset is not None and r.asset != asset:
                continue
            if direction is not None and r.direction != direction:
                continue
            out.append(r)
        return out


def _result(
    *,
    minutes_ago: int = 10,
    status: str = "completed",
    direction: str = "exchange_to_bank",
    asset: str = "USD",
    amount: str = "100",
) -> TransferResult:
    return TransferResult(
        proposal_id="p",
        transaction_id=f"tx-{minutes_ago}-{status}",
        status=status,  # type: ignore[arg-type]
        executed_amount=Decimal(amount),
        direction=direction,  # type: ignore[arg-type]
        asset=asset,
        timestamp=Timestamp(dt=datetime.now(UTC) - timedelta(minutes=minutes_ago)),
    )


@pytest.mark.asyncio
class TestComputeTodayTotalWithdrawnUsd:
    async def test_empty_history_returns_zero(self) -> None:
        history = _StubHistory(results=[])
        total = await compute_today_total_withdrawn_usd(history)
        assert total == Decimal("0")

    async def test_sums_completed_within_window(self) -> None:
        history = _StubHistory(
            results=[
                _result(minutes_ago=60, amount="100"),
                _result(minutes_ago=120, amount="50"),
                _result(minutes_ago=300, amount="25"),
            ],
        )
        total = await compute_today_total_withdrawn_usd(history)
        assert total == Decimal("175")

    async def test_excludes_outside_window(self) -> None:
        """A withdrawal from 25 hours ago is outside the rolling 24h
        window and must not count."""
        history = _StubHistory(
            results=[
                _result(minutes_ago=60, amount="100"),
                _result(minutes_ago=25 * 60, amount="999"),  # outside window
            ],
        )
        total = await compute_today_total_withdrawn_usd(history)
        assert total == Decimal("100")

    async def test_excludes_failed_status(self) -> None:
        """Failed withdrawals didn't move money — must not count."""
        history = _StubHistory(
            results=[
                _result(minutes_ago=60, amount="100", status="completed"),
                _result(minutes_ago=120, amount="50", status="failed"),
                _result(minutes_ago=180, amount="25", status="pending"),
            ],
        )
        total = await compute_today_total_withdrawn_usd(history)
        # 100 completed + 25 pending = 125; failed 50 excluded.
        assert total == Decimal("125")

    async def test_filters_to_outflows(self) -> None:
        """bank→exchange (inflow) doesn't count toward the withdrawal
        cap — verified by the storage filter (helper passes
        direction='exchange_to_bank')."""
        history = _StubHistory(
            results=[
                _result(direction="exchange_to_bank", amount="100"),
                _result(direction="bank_to_exchange", amount="999"),
            ],
        )
        total = await compute_today_total_withdrawn_usd(history)
        assert total == Decimal("100")
        # Verified at the query-arg layer too.
        assert history.last_direction == "exchange_to_bank"

    async def test_filters_to_asset(self) -> None:
        history = _StubHistory(
            results=[
                _result(asset="USD", amount="100"),
                _result(asset="EUR", amount="999"),
            ],
        )
        total = await compute_today_total_withdrawn_usd(history, asset="USD")
        assert total == Decimal("100")
        assert history.last_asset == "USD"

    async def test_now_override_for_deterministic_tests(self) -> None:
        """The ``now`` parameter lets tests pin the window to a fixed
        wall-clock — useful when the absolute time matters."""
        anchor = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        # Result 30min before anchor is inside the 24h window
        result_in = TransferResult(
            proposal_id="p",
            transaction_id="in",
            status="completed",
            executed_amount=Decimal("50"),
            direction="exchange_to_bank",
            asset="USD",
            timestamp=Timestamp(dt=anchor - timedelta(minutes=30)),
        )
        # Result 25h before anchor is outside
        result_out = TransferResult(
            proposal_id="p",
            transaction_id="out",
            status="completed",
            executed_amount=Decimal("100"),
            direction="exchange_to_bank",
            asset="USD",
            timestamp=Timestamp(dt=anchor - timedelta(hours=25)),
        )
        history = _StubHistory(results=[result_in, result_out])
        total = await compute_today_total_withdrawn_usd(history, now=anchor)
        assert total == Decimal("50")

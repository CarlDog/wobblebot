"""Non-trade income aggregation (ADR-040 follow-up).

Anchored on the real 2026-08-22 account. The numbers below are Kraken's,
verified against its Ledgers endpoint AND independently against the
balance-vs-replay deltas they explain:

    SOL  gross 0.0019579684  fee 0.0005873901  net 0.0013705783
    ETH  gross 0.0000609837  fee 0.0000182946  net 0.0000426891
    ADA  gross 0.89881202    fee 0.26964355    net 0.62916847
    BABY gross 0.32424       fee 0.09720       net 0.22704

Kraken bills staking at exactly 30%. Summing gross alone overstates
income by nearly a third — that error is what made the balances look
like they still disagreed during the investigation, so it gets a test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from wobblebot.domain.models import LedgerEntry
from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.staking_income import (
    NON_INCOME_TYPES,
    income_by_asset,
    value_income_usd,
)

_T = Timestamp(dt=datetime(2026, 8, 22, 12, 0, tzinfo=UTC))


def _entry(
    asset: str, etype: str, amount: str, fee: str = "0", eid: str | None = None
) -> LedgerEntry:
    return LedgerEntry(
        id=eid or f"L-{asset}-{etype}-{amount}",
        ref_id="R-1",
        asset=asset,
        entry_type=etype,
        amount=Decimal(amount),
        fee=Decimal(fee),
        occurred_at=_T,
    )


class TestRealAccountFigures:
    """The four assets this account actually earns on."""

    def test_net_matches_the_measured_balance_deltas(self) -> None:
        entries = [
            _entry("SOL", "staking", "0.0019579684", "0.0005873901"),
            _entry("ETH", "staking", "0.0000609837", "0.0000182946"),
            _entry("ADA", "staking", "0.89881202", "0.26964355"),
        ]
        income = income_by_asset(entries)
        # Each equals the gap between the Kraken balance and the
        # replayed quantity, measured independently.
        assert income["SOL"].net == Decimal("0.0013705783")
        assert income["ETH"].net == Decimal("0.0000426891")
        assert income["ADA"].net == Decimal("0.62916847")

    def test_exchange_takes_thirty_percent(self) -> None:
        income = income_by_asset([_entry("ADA", "staking", "0.89881202", "0.26964355")])
        fraction = income["ADA"].fee_fraction
        assert fraction is not None
        assert round(fraction, 4) == Decimal("0.3000")

    def test_gross_alone_overstates_income(self) -> None:
        """The specific error that stalled the 2026-08-22 diagnosis."""
        income = income_by_asset([_entry("ADA", "staking", "0.89881202", "0.26964355")])
        assert income["ADA"].gross > income["ADA"].net
        assert income["ADA"].gross - income["ADA"].net == Decimal("0.26964355")

    def test_untraded_asset_still_counts(self) -> None:
        """BABY is staked but never traded and absent from live.symbols.

        A per-traded-symbol ingest would miss it entirely; aggregation
        must not assume the income assets are the trading universe.
        """
        income = income_by_asset([_entry("BABY", "staking", "0.32424", "0.09720")])
        assert income["BABY"].net == Decimal("0.22704")


class TestClassification:
    def test_trades_are_never_income(self) -> None:
        """Counting them would double every fill already in `trades`."""
        assert income_by_asset([_entry("SOL", "trade", "5.0")]) == {}

    def test_deposits_are_capital_not_income(self) -> None:
        """The operator funding the account is not the account earning.

        Real case: four USD deposits totalling $350 sit in this ledger.
        """
        assert income_by_asset([_entry("USD", "deposit", "100.0")]) == {}

    def test_unknown_types_count_as_income(self) -> None:
        """A denylist, deliberately. An allowlist would silently drop a
        reward type the exchange adds later — the exact loss this
        module exists to end."""
        income = income_by_asset([_entry("SOL", "some_new_reward_type", "1.5")])
        assert income["SOL"].net == Decimal("1.5")
        assert income["SOL"].entry_types == ("some_new_reward_type",)

    def test_negative_entries_are_not_netted_away(self) -> None:
        """An outflow is a loss, not smaller earnings. Netting it into
        an income figure would under-report both."""
        income = income_by_asset(
            [_entry("SOL", "staking", "1.0", eid="a"), _entry("SOL", "penalty", "-0.4", eid="b")]
        )
        assert income["SOL"].gross == Decimal("1.0")
        assert income["SOL"].entry_count == 1

    def test_trade_is_in_the_denylist(self) -> None:
        assert "trade" in NON_INCOME_TYPES
        assert "deposit" in NON_INCOME_TYPES


class TestValuation:
    def test_values_net_not_gross(self) -> None:
        income = income_by_asset([_entry("ADA", "staking", "1.0", "0.3")])
        total, unpriced = value_income_usd(income, {"ADA": Decimal("2")})
        assert total == Decimal("1.4")  # 0.7 net x 2
        assert unpriced == ()

    def test_unpriced_asset_is_named_not_zeroed(self) -> None:
        """BABY has no price snapshot — it is staked but never traded.

        Valuing it at zero would read as "no income" and hide exactly
        what this feature measures.
        """
        income = income_by_asset(
            [_entry("ADA", "staking", "1.0", eid="a"), _entry("BABY", "staking", "5.0", eid="b")]
        )
        total, unpriced = value_income_usd(income, {"ADA": Decimal("2")})
        assert total == Decimal("2")
        assert unpriced == ("BABY",)

    def test_no_prices_yields_zero_and_names_everything(self) -> None:
        income = income_by_asset([_entry("SOL", "staking", "1.0")])
        total, unpriced = value_income_usd(income, {})
        assert total == Decimal(0)
        assert unpriced == ("SOL",)

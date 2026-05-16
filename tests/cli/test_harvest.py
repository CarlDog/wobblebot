"""Tests for cli/harvest — Stage 4.2 read-only treasury monitor."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.harvest import _classify_band, _read_usd_balance, _run_cycle
from wobblebot.config.cli import HarvestConfig
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.config.schedules import SchedulesConfig
from wobblebot.domain.models import Balance
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.exchange import ExchangePort

pytestmark = pytest.mark.unit


# ----- Test doubles -----


class _StubExchange(ExchangePort):
    """ExchangePort stub returning a canned USD balance.

    Only ``get_balance`` is exercised by the harvest daemon, but
    ExchangePort has many abstract methods; the rest raise
    ``NotImplementedError`` if anything other than the harvest path
    calls them — surfaces accidental cross-wiring as a hard failure.
    """

    def __init__(
        self,
        *,
        usd_balance: Decimal | None = Decimal("0"),
        error: ExchangeError | None = None,
    ) -> None:
        self._usd_balance = usd_balance
        self._error = error
        self.call_count = 0

    async def get_balance(self, asset: str) -> Balance | None:
        self.call_count += 1
        if self._error is not None:
            raise self._error
        if asset != "USD" or self._usd_balance is None:
            return None
        return Balance(
            asset="USD",
            total=self._usd_balance,
            available=self._usd_balance,
            locked=Decimal("0"),
        )

    async def get_balances(self) -> list[Balance]:
        raise NotImplementedError("not used by harvest")

    async def get_current_price(self, symbol):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def place_order(self, order):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def cancel_order(self, order):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def get_open_orders(self, symbol=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def get_order_status(self, order):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def get_trade_history(self, symbol=None, since=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used by harvest")

    async def withdraw(self, asset, amount, destination):  # type: ignore[no-untyped-def]
        # Critical: Stage 4.2 must NEVER call withdraw. If anything in
        # the harvest path tries to, this raises and the test catches it.
        raise NotImplementedError("Stage 4.2 daemon must not call withdraw — that's 4.4+ territory")


def _harvester_config() -> HarvesterConfig:
    return HarvesterConfig(
        enabled=False,
        min_exchange_liquidity_usd=Decimal("200"),
        topup_threshold_usd=Decimal("250"),
        surplus_threshold_usd=Decimal("500"),
        max_withdrawal_per_day_usd=Decimal("1000"),
    )


def _grid_config() -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            order_size_usd=Decimal("10"),
        ),
    )


def _safety_config() -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("100"),
        max_daily_spend_usd=Decimal("100"),
        max_per_coin_exposure_usd=Decimal("50"),
        max_orders_per_coin=10,
        emergency_stop=EmergencyStopConfig(
            max_loss_percentage=Decimal("5"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


def _full_config(*, harvester: HarvesterConfig | None = None) -> WobbleBotConfig:
    return WobbleBotConfig(
        grid=_grid_config(),
        safety=_safety_config(),
        schedules=SchedulesConfig(root={"harvest": __import__("datetime").timedelta(minutes=1)}),
        harvest=HarvestConfig(),
        harvester=harvester if harvester is not None else _harvester_config(),
    )


# ----- _read_usd_balance -----


@pytest.mark.asyncio
class TestReadUsdBalance:
    async def test_returns_decimal_balance_on_success(self) -> None:
        adapter = _StubExchange(usd_balance=Decimal("300"))
        result = await _read_usd_balance(adapter)
        assert result == Decimal("300")

    async def test_returns_zero_when_no_usd_balance(self) -> None:
        """A None return from get_balance (no USD on the account) is
        legitimate state — coerce to Decimal('0') so the decision
        logic gets a well-defined input."""
        adapter = _StubExchange(usd_balance=None)
        result = await _read_usd_balance(adapter)
        assert result == Decimal("0")

    async def test_returns_none_on_exchange_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Transport / parse failures are recoverable misses — the
        daemon's outer loop tries again next tick."""
        adapter = _StubExchange(error=ExchangeError("HTTP 502"))
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.harvest"):
            result = await _read_usd_balance(adapter)
        assert result is None
        assert any("balance read failed" in r.message for r in caplog.records)


# ----- _classify_band -----


class TestClassifyBand:
    def test_deficit(self) -> None:
        assert _classify_band(Decimal("100"), _harvester_config()) == "deficit"

    def test_topup_band(self) -> None:
        assert _classify_band(Decimal("210"), _harvester_config()) == "topup_band"

    def test_hold_band_low_edge(self) -> None:
        assert _classify_band(Decimal("250"), _harvester_config()) == "hold_band"

    def test_hold_band_high_edge(self) -> None:
        assert _classify_band(Decimal("500"), _harvester_config()) == "hold_band"

    def test_surplus(self) -> None:
        assert _classify_band(Decimal("600"), _harvester_config()) == "surplus"


# ----- _run_cycle -----


@pytest.mark.asyncio
class TestRunCycleHappyPath:
    async def test_hold_band_logs_no_proposal(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = _StubExchange(usd_balance=Decimal("375"))
        config = _full_config()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=None)
        assert ok is True
        no_proposal = [r for r in caplog.records if "no proposal" in r.message]
        assert no_proposal
        # Structured fields include band classification.
        assert any(getattr(r, "band", None) == "hold_band" for r in no_proposal)

    async def test_surplus_logs_hypothetical_proposal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _StubExchange(usd_balance=Decimal("600"))
        config = _full_config()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=None)
        assert ok is True
        proposal_logs = [r for r in caplog.records if "HYPOTHETICAL" in r.message]
        assert proposal_logs
        # Direction is correct for surplus → exchange_to_bank.
        record = proposal_logs[0]
        assert getattr(record, "direction") == "exchange_to_bank"
        assert getattr(record, "asset") == "USD"
        # Amount = 600 - 375 (midpoint) = 225.
        assert getattr(record, "amount") == "225"

    async def test_topup_band_logs_hypothetical_proposal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _StubExchange(usd_balance=Decimal("210"))
        config = _full_config()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=None)
        assert ok is True
        proposal_logs = [r for r in caplog.records if "HYPOTHETICAL" in r.message]
        assert proposal_logs
        record = proposal_logs[0]
        assert getattr(record, "direction") == "bank_to_exchange"
        # Amount = 375 (midpoint) - 210 = 165.
        assert getattr(record, "amount") == "165"

    async def test_deficit_logs_no_proposal(self, caplog: pytest.LogCaptureFixture) -> None:
        """Below the floor is operator-only territory — the daemon
        produces a tick log but no proposal."""
        adapter = _StubExchange(usd_balance=Decimal("100"))
        config = _full_config()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=None)
        assert ok is True
        no_proposal = [r for r in caplog.records if "no proposal" in r.message]
        assert no_proposal
        assert any(getattr(r, "band", None) == "deficit" for r in no_proposal)


@pytest.mark.asyncio
class TestRunCycleFaultIsolation:
    async def test_balance_read_failure_returns_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bad balance read returns False so the outer loop continues
        without crashing the daemon."""
        adapter = _StubExchange(error=ExchangeError("HTTP 503"))
        config = _full_config()
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=None)
        assert ok is False
        # No proposal log should fire on a failed read.
        assert not [r for r in caplog.records if "HYPOTHETICAL" in r.message]


# ----- Money-safety end-to-end -----


@pytest.mark.asyncio
class TestNoMoneyMovesAtStage42:
    """Cross-cutting sanity: even when the cycle produces a proposal,
    nothing actually moves money. The proposal is purely logged.
    Stage 4.4 introduces real execution."""

    async def test_proposal_log_is_advisory_only(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = _StubExchange(usd_balance=Decimal("600"))
        config = _full_config()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.harvest"):
            await _run_cycle(adapter, config=config, storage=None)
        # The message MUST flag the hypothetical nature so an operator
        # glancing at logs doesn't mistake it for a real action.
        proposal_logs = [r for r in caplog.records if "HYPOTHETICAL" in r.message]
        assert proposal_logs
        assert "no money moved" in proposal_logs[0].message
        # Only ONE call to get_balance (the read); no execute_transfer-shaped path.
        assert adapter.call_count == 1


@pytest.mark.asyncio
class TestPersistence:
    """Stage 4.3: when storage is provided, proposals land in
    transfer_proposals. Persistence is independent of
    HarvesterConfig.enabled — that flag gates execution (4.4+)."""

    async def test_proposal_persists_when_storage_provided(self) -> None:
        storage = SQLiteStorageAdapter(":memory:")
        await storage.connect()
        try:
            adapter = _StubExchange(usd_balance=Decimal("600"))
            config = _full_config()
            ok = await _run_cycle(adapter, config=config, storage=storage)
            assert ok is True

            proposals = await storage.get_transfer_proposals()
            assert len(proposals) == 1
            assert proposals[0].direction == "exchange_to_bank"
            assert proposals[0].amount == Decimal("225")
        finally:
            await storage.close()

    async def test_no_proposal_no_persist(self) -> None:
        """Hold-band ticks return None — nothing should land in storage."""
        storage = SQLiteStorageAdapter(":memory:")
        await storage.connect()
        try:
            adapter = _StubExchange(usd_balance=Decimal("375"))  # hold band
            config = _full_config()
            ok = await _run_cycle(adapter, config=config, storage=storage)
            assert ok is True

            proposals = await storage.get_transfer_proposals()
            assert proposals == []
        finally:
            await storage.close()

    async def test_persistence_independent_of_enabled_flag(self) -> None:
        """harvester.enabled=False (default) does NOT suppress
        persistence — the forensic record is always on. enabled is
        the 4.4 execute-gate, not the persist-gate."""
        storage = SQLiteStorageAdapter(":memory:")
        await storage.connect()
        try:
            disabled_harvester = HarvesterConfig(
                enabled=False,  # explicit
                min_exchange_liquidity_usd=Decimal("200"),
                topup_threshold_usd=Decimal("250"),
                surplus_threshold_usd=Decimal("500"),
                max_withdrawal_per_day_usd=Decimal("1000"),
            )
            adapter = _StubExchange(usd_balance=Decimal("600"))
            config = _full_config(harvester=disabled_harvester)
            await _run_cycle(adapter, config=config, storage=storage)

            proposals = await storage.get_transfer_proposals()
            assert len(proposals) == 1
        finally:
            await storage.close()

    async def test_day_cap_reads_real_history(self) -> None:
        """Stage 4.4b: cli/harvest now queries transfer_results before
        calling propose_transfer, so a withdrawal earlier today
        constrains the next proposal."""
        from datetime import UTC, datetime, timedelta

        from wobblebot.domain.value_objects import Timestamp
        from wobblebot.ports.harvester import TransferResult

        storage = SQLiteStorageAdapter(":memory:")
        await storage.connect()
        try:
            # Seed a 1h-old completed withdrawal of $950. Day-cap is
            # $1000, so only $50 remains.
            past = TransferResult(
                proposal_id="prior",
                transaction_id="tx-earlier-today",
                status="completed",
                executed_amount=Decimal("950"),
                direction="exchange_to_bank",
                asset="USD",
                timestamp=Timestamp(dt=datetime.now(UTC) - timedelta(hours=1)),
            )
            await storage.save_transfer_result(past)

            # Balance way above surplus — would propose a $625 scrape
            # if there were no history (1000 - 375 midpoint). With
            # the seeded $950 already withdrawn, only $50 fits.
            adapter = _StubExchange(usd_balance=Decimal("1000"))
            config = _full_config()
            await _run_cycle(adapter, config=config, storage=storage)

            proposals = await storage.get_transfer_proposals()
            assert len(proposals) == 1
            assert proposals[0].amount == Decimal("50")
            assert "max_withdrawal_per_day_usd" in proposals[0].rationale
        finally:
            await storage.close()

    async def test_storage_failure_logged_but_cycle_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A storage write failure must NOT kill the daemon — the log
        stream is the operator's primary surface; missing one audit
        row is less bad than missing every subsequent tick."""

        class _FlakeyStorage:
            """Just-enough SQLiteStorageAdapter-shaped object that
            raises StorageError on proposal-save but answers the
            day-cap history read normally (returns empty)."""

            async def get_transfer_results(  # type: ignore[no-untyped-def]
                self,
                since=None,
                status=None,
                asset=None,
                direction=None,
                limit=None,
            ):
                return []

            async def save_transfer_proposal(self, proposal):  # type: ignore[no-untyped-def]
                from wobblebot.ports.exceptions import StorageError as _SE

                raise _SE("simulated db down")

        adapter = _StubExchange(usd_balance=Decimal("600"))
        config = _full_config()
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.harvest"):
            ok = await _run_cycle(adapter, config=config, storage=_FlakeyStorage())  # type: ignore[arg-type]
        assert ok is True  # cycle succeeded even though persist failed
        assert any("persistence failed" in r.message for r in caplog.records)

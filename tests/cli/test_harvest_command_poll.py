"""ADR-034 — cli/harvest's approved-command poll (the web money-out path).

Covers the half of ADR-034 that lives in the daemon: the kind-scoped
SELECT, the echo-validation gate, the row lifecycle, and the invariant
that matters most — an approved row is the ONLY thing that reaches
``adapter.withdraw()``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.cli.test_harvest import (  # reuse the money-path doubles
    _enabled_harvester,
    _full_config,
    _proposal,
    _WithdrawingExchange,
)
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.harvest_execute import _process_pending_commands
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.operator import (
    ExecuteProposalCommand,
    PauseCommand,
    PendingCommand,
)


def _pending(
    command: object,
    *,
    status: str = "approved",
) -> PendingCommand:
    now = datetime.now(UTC)
    return PendingCommand(
        id=uuid4(),
        command=command,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        channel_id="web",
        requesting_user_id="operator",
        ttl_expires_at=Timestamp(dt=now + timedelta(minutes=10)),
        created_at=Timestamp(dt=now),
    )


def _execute_command_for(
    proposal_id: str = "p-test",
    *,
    amount: str = "100",
    destination: str = "test-bank-label",
) -> ExecuteProposalCommand:
    return ExecuteProposalCommand(
        proposal_id=proposal_id,
        amount_usd=Decimal(amount),
        destination=destination,
    )


@pytest.mark.asyncio
class TestKindScopedPoll:
    """The SELECT must claim execute_proposal rows and nothing else."""

    async def test_ignores_engine_commands(self) -> None:
        """A pause row is cli/live's; harvest must leave it untouched."""
        operator_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        try:
            pause = _pending(PauseCommand(symbol=Symbol(base="BTC", quote="USD")))
            await operator_storage.save_pending_command(pause)

            processed = await _process_pending_commands(
                adapter=_WithdrawingExchange(),
                storage=None,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 0
            # Still approved and untouched — cli/live can still claim it.
            row = await operator_storage.get_pending_command(pause.id)
            assert row is not None
            assert row.status == "approved"
            assert row.result is None
        finally:
            await operator_storage.close()

    async def test_ignores_unapproved_execute_rows(self) -> None:
        """The confirm gate: awaiting_confirmation never executes."""
        operator_storage = SQLiteStorageAdapter(":memory:")
        harvest_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        await harvest_storage.connect()
        try:
            await harvest_storage.save_transfer_proposal(_proposal())
            row = _pending(_execute_command_for(), status="awaiting_confirmation")
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()

            processed = await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 0
            assert adapter.withdraw_calls == []
        finally:
            await operator_storage.close()
            await harvest_storage.close()


@pytest.mark.asyncio
class TestEchoValidation:
    """The approval must describe the transfer that actually executes."""

    async def test_amount_mismatch_refuses(self) -> None:
        operator_storage = SQLiteStorageAdapter(":memory:")
        harvest_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        await harvest_storage.connect()
        try:
            # Operator approved $100; the stored proposal now says $900.
            await harvest_storage.save_transfer_proposal(_proposal(amount="900"))
            row = _pending(_execute_command_for(amount="100"))
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()

            processed = await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 1
            assert adapter.withdraw_calls == []
            updated = await operator_storage.get_pending_command(row.id)
            assert updated is not None
            assert updated.status == "failed"
            assert updated.result is not None
            assert "100" in updated.result.message and "900" in updated.result.message
        finally:
            await operator_storage.close()
            await harvest_storage.close()

    async def test_destination_mismatch_refuses(self) -> None:
        operator_storage = SQLiteStorageAdapter(":memory:")
        harvest_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        await harvest_storage.connect()
        try:
            await harvest_storage.save_transfer_proposal(_proposal())
            row = _pending(_execute_command_for(destination="some-other-bank"))
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()

            processed = await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 1
            assert adapter.withdraw_calls == []
            updated = await operator_storage.get_pending_command(row.id)
            assert updated is not None
            assert updated.status == "failed"
            assert "some-other-bank" in updated.result.message  # type: ignore[union-attr]
        finally:
            await operator_storage.close()
            await harvest_storage.close()


@pytest.mark.asyncio
class TestApprovedExecution:
    """The happy path and its audit trail."""

    async def test_approved_row_executes_and_marks_dispatched(self) -> None:
        operator_storage = SQLiteStorageAdapter(":memory:")
        harvest_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        await harvest_storage.connect()
        try:
            await harvest_storage.save_transfer_proposal(_proposal())
            row = _pending(_execute_command_for())
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()

            processed = await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 1
            # Money moved exactly once, with the approved values.
            assert len(adapter.withdraw_calls) == 1
            call = adapter.withdraw_calls[0]
            assert call["amount"] == Decimal("100")
            assert call["destination"] == "test-bank-label"
            updated = await operator_storage.get_pending_command(row.id)
            assert updated is not None
            assert updated.status == "dispatched"
            assert updated.result is not None
            assert updated.result.success is True
            assert updated.result.command_kind == "execute_proposal"
            # The forensic row landed too.
            results = await harvest_storage.get_transfer_results()
            assert [r.proposal_id for r in results] == ["p-test"]
        finally:
            await operator_storage.close()
            await harvest_storage.close()

    async def test_second_poll_does_not_double_withdraw(self) -> None:
        """Idempotency: a re-polled row hits the layer-2b guard."""
        operator_storage = SQLiteStorageAdapter(":memory:")
        harvest_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        await harvest_storage.connect()
        try:
            await harvest_storage.save_transfer_proposal(_proposal())
            row = _pending(_execute_command_for())
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()
            config = _full_config(harvester=_enabled_harvester())

            await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=config,
            )
            # Simulate the persistence-failure hazard: the row is still
            # 'approved' on the next poll.
            await operator_storage.save_pending_command(
                row.model_copy(update={"status": "approved"})
            )
            await _process_pending_commands(
                adapter=adapter,
                storage=harvest_storage,
                operator_storage=operator_storage,
                config=config,
            )

            assert len(adapter.withdraw_calls) == 1
            updated = await operator_storage.get_pending_command(row.id)
            assert updated is not None
            assert updated.status == "failed"
            assert "already executed" in updated.result.message  # type: ignore[union-attr]
        finally:
            await operator_storage.close()
            await harvest_storage.close()

    async def test_missing_harvest_storage_refuses_cleanly(self) -> None:
        """A web-only deployment must refuse, not crash."""
        operator_storage = SQLiteStorageAdapter(":memory:")
        await operator_storage.connect()
        try:
            row = _pending(_execute_command_for())
            await operator_storage.save_pending_command(row)
            adapter = _WithdrawingExchange()

            processed = await _process_pending_commands(
                adapter=adapter,
                storage=None,
                operator_storage=operator_storage,
                config=_full_config(harvester=_enabled_harvester()),
            )

            assert processed == 1
            assert adapter.withdraw_calls == []
            updated = await operator_storage.get_pending_command(row.id)
            assert updated is not None
            assert updated.status == "failed"
        finally:
            await operator_storage.close()

    async def test_no_operator_storage_is_a_noop(self) -> None:
        processed = await _process_pending_commands(
            adapter=_WithdrawingExchange(),
            storage=None,
            operator_storage=None,
            config=_full_config(harvester=_enabled_harvester()),
        )
        assert processed == 0

"""Transition rules for an operator approve/reject decision.

Migrated from ``tests/cli/test_operator.py::TestHandleReaction`` when the
logic moved out of ``_handle_reaction`` into ``services/confirm_decision``
(P3 buttons-over-reactions). Same coverage, now asserted against the
single implementation both the Discord buttons and any future confirming
surface call — which is the whole point of the extraction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.operator import PauseCommand, PendingCommand, StopCommand
from wobblebot.services.confirm_decision import apply_confirm_decision

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _pending(
    *,
    status: str = "awaiting_confirmation",
    ttl_minutes: int = 10,
    command: object | None = None,
    confirming_user_id: str | None = None,
) -> PendingCommand:
    now = datetime.now(UTC)
    return PendingCommand(
        id=uuid4(),
        command=command or PauseCommand(symbol=BTC_USD),  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        channel_id="C-1",
        requesting_user_id="U-1",
        confirming_user_id=confirming_user_id,
        confirmed_at=Timestamp(dt=now) if confirming_user_id else None,
        ttl_expires_at=Timestamp(dt=now + timedelta(minutes=ttl_minutes)),
        created_at=Timestamp(dt=now),
    )


class TestDecisions:
    async def test_approve_transitions_to_approved(self, storage: SQLiteStorageAdapter) -> None:
        row = _pending()
        await storage.save_pending_command(row)

        outcome = await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="approve", user_id="U-2"
        )

        assert outcome.result == "approved"
        assert outcome.decided is True
        fetched = await storage.get_pending_command(row.id)
        assert fetched is not None
        assert fetched.status == "approved"
        assert fetched.confirming_user_id == "U-2"
        assert fetched.confirmed_at is not None

    async def test_reject_transitions_to_rejected(self, storage: SQLiteStorageAdapter) -> None:
        row = _pending(command=StopCommand())
        await storage.save_pending_command(row)

        outcome = await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="reject", user_id="U-2"
        )

        assert outcome.result == "rejected"
        fetched = await storage.get_pending_command(row.id)
        assert fetched is not None
        assert fetched.status == "rejected"


class TestGuards:
    async def test_decision_after_ttl_expires_instead_of_approving(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A click that lands between expirer sweeps must not approve.

        Mirrors the web route's fix (fleet-review #19 finding 6): the row
        goes to ``expired`` with NO confirming user, because nothing was
        actually confirmed in time.
        """
        row = _pending(ttl_minutes=-1)
        await storage.save_pending_command(row)

        outcome = await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="approve", user_id="U-2"
        )

        assert outcome.result == "expired"
        assert outcome.decided is False
        fetched = await storage.get_pending_command(row.id)
        assert fetched is not None
        assert fetched.status == "expired"
        assert fetched.confirming_user_id is None
        assert fetched.confirmed_at is None

    async def test_second_decision_does_not_overwrite_the_first(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A reject after an approve is a no-op, not a flip."""
        row = _pending(status="approved", confirming_user_id="U-2")
        await storage.save_pending_command(row)

        outcome = await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="reject", user_id="U-3"
        )

        assert outcome.result == "already_decided"
        fetched = await storage.get_pending_command(row.id)
        assert fetched is not None
        assert fetched.status == "approved"
        assert fetched.confirming_user_id == "U-2"

    async def test_unknown_id_is_reported_not_raised(self, storage: SQLiteStorageAdapter) -> None:
        outcome = await apply_confirm_decision(
            storage=storage, pending_id=uuid4(), decision="approve", user_id="U-2"
        )
        assert outcome.result == "not_found"
        assert outcome.decided is False

    async def test_storage_failure_is_reported_not_raised(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Every caller is an event handler; a bad row must not kill it."""
        row = _pending()
        await storage.save_pending_command(row)

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise StorageError("disk gone")

        storage.save_pending_command = _boom  # type: ignore[method-assign]

        outcome = await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="approve", user_id="U-2"
        )

        assert outcome.result == "error"
        assert "disk gone" in outcome.message


class TestFirewall:
    async def test_approving_never_dispatches(self, storage: SQLiteStorageAdapter) -> None:
        """ADR-002/ADR-013: this only moves the row; a daemon executes.

        The assertion that matters is the terminal state: ``approved``,
        NOT ``dispatched``. If this function ever grew an execution path,
        this is the test that would catch it.
        """
        row = _pending()
        await storage.save_pending_command(row)

        await apply_confirm_decision(
            storage=storage, pending_id=row.id, decision="approve", user_id="U-2"
        )

        fetched = await storage.get_pending_command(row.id)
        assert fetched is not None
        assert fetched.status == "approved"
        assert fetched.dispatched_at is None
        assert fetched.result is None

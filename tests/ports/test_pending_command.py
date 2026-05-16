"""Unit tests for ``PendingCommand`` (Stage 5.1.A).

The lifecycle state machine itself lands in Stage 5.4 alongside the
``pending_commands`` SQLite table. These tests cover the *type shape*
the table will mirror: construction, required fields, frozen
immutability, the ``PendingCommandStatus`` literal, and JSON
round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.operator import (
    CommandResult,
    PauseCommand,
    PendingCommand,
    StopCommand,
)

pytestmark = pytest.mark.unit


_btc = Symbol(base="BTC", quote="USD")


def _ts(offset_seconds: int = 0) -> Timestamp:
    return Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _minimal_pending(**overrides: object) -> PendingCommand:
    base: dict[str, object] = {
        "id": uuid4(),
        "command": PauseCommand(symbol=_btc),
        "channel_id": "C-1",
        "requesting_user_id": "U-1",
        "ttl_expires_at": _ts(300),
        "created_at": _ts(0),
    }
    base.update(overrides)
    return PendingCommand(**base)  # type: ignore[arg-type]


class TestPendingCommandConstruction:
    def test_minimal(self) -> None:
        pending = _minimal_pending()
        assert pending.status == "awaiting_confirmation"
        assert isinstance(pending.command, PauseCommand)
        assert pending.confirming_user_id is None
        assert pending.confirmed_at is None
        assert pending.dispatched_at is None
        assert pending.result is None

    def test_frozen(self) -> None:
        pending = _minimal_pending()
        with pytest.raises(ValidationError):
            pending.status = "approved"  # type: ignore[misc]

    def test_channel_id_required(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_pending(channel_id="")

    def test_user_id_required(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_pending(requesting_user_id="")

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_pending(status="wishlist")


class TestPendingCommandTransitionShapes:
    """Status-transition outcomes captured as type-level snapshots.

    Stage 5.4 owns the state-machine code; these tests just verify the
    type permits the expected terminal shapes.
    """

    def test_approved_shape(self) -> None:
        pending = _minimal_pending(
            status="approved",
            confirming_user_id="U-2",
            confirmed_at=_ts(60),
        )
        assert pending.status == "approved"
        assert pending.confirming_user_id == "U-2"

    def test_dispatched_shape(self) -> None:
        pending = _minimal_pending(
            status="dispatched",
            confirming_user_id="U-2",
            confirmed_at=_ts(60),
            dispatched_at=_ts(70),
            result=CommandResult(
                success=True,
                command_kind="pause",
                message="BTC paused",
                executed_at=_ts(71),
            ),
        )
        assert pending.status == "dispatched"
        assert pending.result is not None
        assert pending.result.success is True

    def test_failed_shape(self) -> None:
        pending = _minimal_pending(
            status="failed",
            confirming_user_id="U-2",
            confirmed_at=_ts(60),
            dispatched_at=_ts(70),
            result=CommandResult(
                success=False,
                command_kind="pause",
                message="symbol unknown",
                executed_at=_ts(71),
            ),
        )
        assert pending.status == "failed"
        assert pending.result is not None
        assert pending.result.success is False

    def test_rejected_shape(self) -> None:
        pending = _minimal_pending(status="rejected")
        assert pending.status == "rejected"
        # Rejected rows have no dispatched/result fields set.
        assert pending.dispatched_at is None

    def test_expired_shape(self) -> None:
        pending = _minimal_pending(status="expired")
        assert pending.status == "expired"


class TestPendingCommandRoundTrip:
    def test_round_trip_with_stop_command(self) -> None:
        pending = _minimal_pending(command=StopCommand())
        adapter = TypeAdapter(PendingCommand)
        dumped = adapter.dump_python(pending)
        revived = adapter.validate_python(dumped)
        assert revived == pending

    def test_round_trip_dispatched(self) -> None:
        pending = _minimal_pending(
            status="dispatched",
            confirming_user_id="U-2",
            confirmed_at=_ts(60),
            dispatched_at=_ts(70),
            result=CommandResult(
                success=True,
                command_kind="pause",
                message="paused",
                executed_at=_ts(71),
            ),
        )
        adapter = TypeAdapter(PendingCommand)
        dumped = adapter.dump_python(pending)
        revived = adapter.validate_python(dumped)
        assert revived == pending

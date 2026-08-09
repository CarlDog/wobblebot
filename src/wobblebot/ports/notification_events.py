"""Typed proactive-notification events (P3 renderers slice, Approach B).

The seven events ``cli/live`` / ``cli/harvest`` raise through
``NotifierPort`` — plus ``command_result``, the dispatch echo added by
the 2026-08-09 re-anchor e2e finding — as a discriminated union
mirroring ``OperatorCommand`` / ``QueryResult``. A typed event rides
inside ``Notification.event``; the storage layer serializes it into
the existing ``context_json`` column (no schema migration) and the
forwarder dispatches typed rows to the bespoke embed renderer.

Rows written before this slice carry a plain context dict (no
``kind`` key) and deserialize to ``event=None`` — the legacy render
path. The heartbeat-alert and maintenance notifications stay on that
path deliberately: their titles/messages are already purpose-written,
and typing them is renderer work without a payoff today.

Two latent inconsistencies from the untyped context dicts are fixed
here per the blueprint: ``session_start`` carries ``symbols`` (a
sequence, not a joined string), and ``session_end``'s ``"unknown"``
sentinel strings become real ``None``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class _FrozenEvent(BaseModel):
    """Shared config: events are immutable value objects."""

    class Config:
        frozen = True


class SessionStartEvent(_FrozenEvent):
    """cli/live began a trading session."""

    kind: Literal["session_start"] = "session_start"
    symbols: tuple[str, ...]
    tick_seconds: float
    max_runtime_seconds: float | None
    max_session_loss_usd: Decimal
    starting_usd: Decimal
    starting_value_usd: Decimal


class FillEvent(_FrozenEvent):
    """One or more orders filled on a symbol this tick."""

    kind: Literal["fill"] = "fill"
    symbol: str
    fills: int
    counters_placed: int
    tick: int


class LossCapEvent(_FrozenEvent):
    """The session loss cap tripped; cli/live is stopping."""

    kind: Literal["loss_cap"] = "loss_cap"
    session_pnl_usd: Decimal
    limit_usd: Decimal
    tick: int


class SessionEndEvent(_FrozenEvent):
    """cli/live ended a session (clean or not — see ``exit_code``).

    The ending balances are ``None`` when the final balance fetch
    failed (the old context dict spelled this ``"unknown"``).
    """

    kind: Literal["session_end"] = "session_end"
    ticks: int
    duration_seconds: float
    starting_usd: Decimal
    ending_usd: Decimal | None
    starting_value_usd: Decimal
    ending_value_usd: Decimal | None
    session_pnl_usd: Decimal | None
    open_orders_cancelled: int
    open_orders_cancel_failed: int
    exit_code: int


class HarvestProposalEvent(_FrozenEvent):
    """The harvester proposed a transfer (hypothetical until executed)."""

    kind: Literal["harvest_proposal"] = "harvest_proposal"
    proposal_id: str
    direction: str
    asset: str
    amount: Decimal
    current_exchange_balance: Decimal
    target_exchange_balance: Decimal
    rationale: str


class WithdrawalFailedEvent(_FrozenEvent):
    """Kraken rejected a withdrawal; no money moved."""

    kind: Literal["withdrawal_failed"] = "withdrawal_failed"
    proposal_id: str
    asset: str
    amount: Decimal
    destination: str
    error: str
    error_type: str


class WithdrawalSubmittedEvent(_FrozenEvent):
    """Kraken accepted a withdrawal — money left the exchange."""

    kind: Literal["withdrawal_submitted"] = "withdrawal_submitted"
    proposal_id: str
    transaction_id: str
    asset: str
    amount: Decimal
    destination: str
    status: str


class CommandResultEvent(_FrozenEvent):
    """An approved operator command was dispatched (the ✅'s receipt).

    Added by the 2026-08-09 re-anchor e2e finding: dispatch used to
    record its ``CommandResult`` only in the ``pending_commands`` row,
    so the operator's approval got silence — which hid a ``placed
    0/6`` re-anchor outcome. ``message`` is the same operator-readable
    audit line the row stores.
    """

    kind: Literal["command_result"] = "command_result"
    command_kind: str
    symbol: str | None
    success: bool
    message: str


NotificationEvent = Annotated[
    Union[
        SessionStartEvent,
        FillEvent,
        LossCapEvent,
        SessionEndEvent,
        HarvestProposalEvent,
        WithdrawalFailedEvent,
        WithdrawalSubmittedEvent,
        CommandResultEvent,
    ],
    Field(discriminator="kind"),
]

__all__ = (
    "CommandResultEvent",
    "FillEvent",
    "HarvestProposalEvent",
    "LossCapEvent",
    "NotificationEvent",
    "SessionEndEvent",
    "SessionStartEvent",
    "WithdrawalFailedEvent",
    "WithdrawalSubmittedEvent",
)

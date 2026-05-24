"""OperatorPort — Abstract interface for operator interaction (Phase 5).

Per ADR-013, Phase 5 introduces the Operator Interaction Engine — a
Discord-based bidirectional surface that lets the operator converse
with WobbleBot, issue commands, and ask status queries in natural
language. Phase 7's web UI is the second client of the same surface.

This module is the engine-side contract. Stage 8.0.A split the type
hierarchy into three focused modules:

- :mod:`wobblebot.ports.operator_intents` — :data:`OperatorCommand`,
  :data:`OperatorQuery`, :data:`OperatorIntent` discriminated unions
  and every variant (PauseCommand ... StopCommand, StatusQuery ...
  HelpQuery, IntentCommand ... IntentUnparseable).
- :mod:`wobblebot.ports.operator_results` — per-query Result types,
  :data:`QueryResult` union, :class:`CommandResult`, and the entry
  types each Result references.
- This module — :class:`PendingCommand` audit-trail model +
  :class:`OperatorPort` ABC.

**Backward compatibility.** Every type that moved out is re-exported
from this module so existing imports
(``from wobblebot.ports.operator import PauseCommand`` etc.) keep
working unchanged. New code MAY import from the focused modules
directly; legacy code doesn't have to.

CRITICAL: per ADR-002 the LLM cannot execute. Every ``Command``
flows through Stage 5.4's pending-command confirmation; only an
operator ✅ reaction transitions a row to ``approved``. There is
no code path from ``OperatorPort.dispatch_command`` to the engine
that does not first see ``status == 'approved'``. The
conversational LLM parses intent; the human gates execution.

Design decisions ratified in ``stage-5.1-design.md`` (do not
relitigate without an ADR):

- Pydantic discriminated unions for every sum type; ``kind:
  Literal[...]`` on each variant.
- Frozen Pydantic models throughout — mutation is
  ``model_copy(update=...)``.
- Command catalog is small and bounded; no live config edits in v1
  (those flow through ``cli/apply``).
- Query catalog is read-only; domain misses are empty lists, not
  exceptions.
- ``Symbol`` accepts both ``"BTC/USD"`` strings and ``{base, quote}``
  dicts via a ``BeforeValidator`` so the LLM can emit either form.
- ``PendingCommand`` carries the lifecycle. The SQLite table that
  persists it lands in Stage 5.4.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp

# Backward-compat re-exports. Every type that moved to the focused
# modules is re-exported here so existing imports keep working.
# Star-imports are deliberate; the re-export contract IS that the
# public API of ``ports.operator`` covers the union of the two
# focused modules. ``__all__`` below lists every name explicitly.
# pylint: disable=wildcard-import,unused-wildcard-import
from wobblebot.ports.operator_intents import *  # noqa: F401,F403
from wobblebot.ports.operator_intents import (
    OperatorCommand,
)
from wobblebot.ports.operator_results import *  # noqa: F401,F403
from wobblebot.ports.operator_results import (
    CommandResult,
    QueryResult,
)

# --------------------------------------------------------------------- #
# PendingCommand — full audit-trail model                               #
# --------------------------------------------------------------------- #


PendingCommandStatus = Literal[
    "awaiting_confirmation",
    "approved",
    "rejected",
    "expired",
    "dispatched",
    "failed",
]
"""Lifecycle states a ``PendingCommand`` moves through.

Created in ``awaiting_confirmation``. ✅ reaction transitions to
``approved``. ❌ → ``rejected``. TTL passes without reaction →
``expired``. ``cli/live`` dispatches ``approved`` rows and marks them
``dispatched`` on success or ``failed`` on engine refusal / error.
Terminal states are ``rejected``, ``expired``, ``dispatched``,
``failed`` (no further transitions). Stage 5.4 owns the table and
the state machine code; Stage 5.1 only encodes the lifecycle as
data here.
"""


class PendingCommand(BaseModel):
    """Audit-trail row for a Stage 5.4 ``pending_commands`` table entry.

    Created by ``cli/operator`` when the assistant emits an
    ``IntentCommand``; consumed by ``cli/live`` after operator
    confirmation. The table itself lands in Stage 5.4 — this type is
    the schema-shaped contract every downstream stage depends on.
    """

    id: UUID
    command: OperatorCommand
    status: PendingCommandStatus = "awaiting_confirmation"
    channel_id: str = Field(min_length=1)
    requesting_user_id: str = Field(min_length=1)
    confirming_user_id: str | None = None
    confirmed_at: Timestamp | None = None
    dispatched_at: Timestamp | None = None
    result: CommandResult | None = None
    ttl_expires_at: Timestamp
    created_at: Timestamp

    class Config:
        frozen = True


# --------------------------------------------------------------------- #
# OperatorPort — engine-side ABC                                        #
# --------------------------------------------------------------------- #


class OperatorPort(ABC):
    """Abstract interface for operator-driven engine interaction.

    Phase 5+ feature — wired to ``services/operator_service.py`` in
    Stage 5.4. ``cli/operator`` consumes the port via constructor DI
    when an operator's confirmed ``PendingCommand`` needs dispatching,
    or when a ``Query`` needs answering against engine + storage state.

    Implementations:
    - ``services.operator_service.OperatorService`` (Stage 5.4)

    Error convention:
    - Domain misses (symbol not active, empty result set, advisor
      suggestion missing) return a structured ``CommandResult`` /
      ``QueryResult`` with the appropriate ``success`` / empty-list
      shape; not an exception.
    - Protocol or transport failures raise ``OperatorError`` (storage
      unreachable, engine method raises unexpectedly).
    """

    @abstractmethod
    async def dispatch_command(self, command: OperatorCommand) -> CommandResult:
        """Execute an approved command against the engine.

        CRITICAL: per ADR-002 + ADR-013, the dispatcher MUST verify
        the originating ``PendingCommand.status == 'approved'`` before
        calling this method. The port itself does not re-check —
        callers route everything through the confirm-before-execute
        flow in Stage 5.4.

        Args:
            command: Concrete ``OperatorCommand`` variant to dispatch.

        Returns:
            ``CommandResult`` describing what the engine did.

        Raises:
            OperatorError: Engine method raises, storage unreachable,
                or other infrastructure failure. Domain refusals
                (e.g. resuming an already-active symbol) are encoded
                in ``CommandResult.success`` rather than raised.
        """

    @abstractmethod
    async def answer_query(
        self,
        query: OperatorQuery,  # noqa: F405
        *,
        channel_id: str | None = None,
        user_id: str | None = None,
    ) -> QueryResult:  # noqa: F405
        """Answer a read-only operator query against current state.

        Args:
            query: Concrete ``OperatorQuery`` variant to answer.
            channel_id: Discord channel scope. Optional — only used by
                ``StatusReportQuery`` (to look up the per-(channel,
                user) "since last status_report" anchor). Other queries
                ignore it.
            user_id: Discord user scope. Same usage as ``channel_id``.

        Returns:
            ``QueryResult`` variant matching the query's ``kind``.

        Raises:
            OperatorError: Storage unreachable or other infrastructure
                failure. Empty result sets are encoded in the result
                type's list fields, not raised.
        """


__all__ = (
    # PendingCommand + lifecycle
    "PendingCommand",
    "PendingCommandStatus",
    # Port
    "OperatorPort",
    # Re-exports from operator_intents (full set)
    "CancelOpenOrdersCommand",
    "GridConfigQuery",
    "HarvesterStatusQuery",
    "HelpQuery",
    "IntentCommand",
    "IntentConversational",
    "IntentQuery",
    "IntentUnparseable",
    "OpenOrdersQuery",
    "OperatorCommand",
    "OperatorIntent",
    "OperatorQuery",
    "OptionalSymbolInput",
    "PauseAllCommand",
    "PauseCommand",
    "RecentFillsQuery",
    "RecentNewsQuery",
    "RecentProposalsQuery",
    "RecentSuggestionsQuery",
    "ResumeAllCommand",
    "ResumeCommand",
    "StatusQuery",
    "StopCommand",
    "SymbolInput",
    # Re-exports from operator_results (full set)
    "CommandResult",
    "FillEntry",
    "GridConfigResult",
    "HarvesterStatusResult",
    "HelpEntry",
    "HelpResult",
    "NewsEntry",
    "OpenOrderEntry",
    "OpenOrdersResult",
    "ProposalEntry",
    "QueryResult",
    "RecentFillsResult",
    "RecentNewsResult",
    "RecentProposalsResult",
    "RecentSuggestionsResult",
    "StatusResult",
    "SuggestionEntry",
    "SymbolStatusEntry",
)

"""OperatorPort - Abstract interface for operator interaction (Phase 5).

Per ADR-013, Phase 5 introduces the Operator Interaction Engine — a
Discord-based bidirectional surface that lets the operator converse with
WobbleBot, issue commands, and ask status queries in natural language.

This module defines the *engine-side* contract:

- ``OperatorIntent`` — a strict typed sum the assistant emits after
  parsing operator text (``Command`` | ``Query`` | ``Conversational`` |
  ``Unparseable``). Same wire-format discipline as ``AdvisorRecommendation``.
- ``OperatorCommand`` — typed sum of state-mutating actions (pause,
  resume, cancel-open-orders, stop). Every command routes through the
  Stage 5.4 confirm-before-execute gate; this port never executes
  without operator approval.
- ``OperatorQuery`` — typed sum of read-only state questions (status,
  open orders, recent fills, recent suggestions, recent news, harvester
  status, recent proposals, grid config, help). Queries execute
  immediately.
- ``CommandResult`` / ``QueryResult`` — typed outputs. Each query has
  its own ``*Result`` type discriminated by the same ``kind`` as the
  source query.
- ``PendingCommand`` — audit-trail model carrying a command through
  its lifecycle (``awaiting_confirmation`` → ``approved`` → ``dispatched``,
  with ``rejected`` / ``expired`` / ``failed`` terminal states).
- ``OperatorPort`` — ABC implemented by ``services/operator_service.py``
  in Stage 5.4. ``cli/operator`` consumes the port via DI.

CRITICAL: per ADR-002 the LLM cannot execute. Every ``Command`` flows
through Stage 5.4's pending-command confirmation; only an operator
``✅`` reaction transitions a row to ``approved``. There is no code
path from ``OperatorPort.dispatch_command`` to the engine that does
not first see ``status == 'approved'``. The conversational LLM
parses intent; the human gates execution.

Design decisions ratified in ``stage-5.1-design.md`` (do not relitigate
without an ADR):

- Pydantic discriminated unions for every sum type; ``kind: Literal[...]``
  on each variant.
- Frozen Pydantic models throughout — mutation is ``model_copy(update=...)``.
- Command catalog is small and bounded; no live config edits in v1
  (those flow through ``cli/apply``).
- Query catalog is read-only; domain misses are empty lists, not
  exceptions.
- ``Symbol`` accepts both ``"BTC/USD"`` strings and ``{base, quote}``
  dicts via a ``BeforeValidator`` so the LLM can emit either form.
- No SQLite table introduced here. ``pending_commands`` lands with
  Stage 5.4 (engine integration) when there's code that reads + writes
  it. Stage 5.1 just defines the type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field

from wobblebot.domain.value_objects import Symbol, Timestamp

# --------------------------------------------------------------------- #
# Symbol coercion helper                                                #
# --------------------------------------------------------------------- #


def _coerce_symbol(value: object) -> object:
    """BeforeValidator: accept ``"BTC/USD"`` strings as well as dicts.

    The LLM emits trading pairs as ``"BTC/USD"`` strings; persisted /
    in-memory uses already pass ``Symbol`` instances or dicts.
    ``None`` passes through for optional fields.
    """
    if isinstance(value, str):
        return Symbol.from_string(value)
    return value


SymbolInput = Annotated[Symbol, BeforeValidator(_coerce_symbol)]
OptionalSymbolInput = Annotated[Symbol | None, BeforeValidator(_coerce_symbol)]


# --------------------------------------------------------------------- #
# OperatorCommand — state-mutating actions                              #
# --------------------------------------------------------------------- #


class PauseCommand(BaseModel):
    """Pause grid trading on one symbol.

    Engine skips the symbol on subsequent ticks. Open orders are
    preserved; ``CancelOpenOrdersCommand`` is a separate action.
    """

    kind: Literal["pause"] = "pause"
    symbol: SymbolInput

    class Config:
        frozen = True


class ResumeCommand(BaseModel):
    """Resume grid trading on one previously paused symbol."""

    kind: Literal["resume"] = "resume"
    symbol: SymbolInput

    class Config:
        frozen = True


class PauseAllCommand(BaseModel):
    """Pause grid trading on every currently active symbol."""

    kind: Literal["pause_all"] = "pause_all"

    class Config:
        frozen = True


class ResumeAllCommand(BaseModel):
    """Resume grid trading on every currently paused symbol."""

    kind: Literal["resume_all"] = "resume_all"

    class Config:
        frozen = True


class CancelOpenOrdersCommand(BaseModel):
    """Cancel open grid orders.

    ``symbol = None`` cancels every open order across every symbol;
    a concrete ``Symbol`` scopes to one. Does not pause the symbol;
    the next tick will re-lay the grid unless ``PauseCommand`` is
    also issued.
    """

    kind: Literal["cancel_open_orders"] = "cancel_open_orders"
    symbol: OptionalSymbolInput = None

    class Config:
        frozen = True


class StopCommand(BaseModel):
    """Request a soft stop of ``cli/live``.

    The engine finishes the current tick, cancels open orders, and
    exits cleanly — same path as SIGINT. Use ``CancelOpenOrdersCommand``
    + ``PauseAllCommand`` if you want to halt trading without killing
    the process.
    """

    kind: Literal["stop"] = "stop"

    class Config:
        frozen = True


OperatorCommand = Annotated[
    PauseCommand
    | ResumeCommand
    | PauseAllCommand
    | ResumeAllCommand
    | CancelOpenOrdersCommand
    | StopCommand,
    Field(discriminator="kind"),
]
"""Discriminated union over all v1 operator-issuable state mutations."""


# --------------------------------------------------------------------- #
# OperatorQuery — read-only state questions                             #
# --------------------------------------------------------------------- #


class StatusQuery(BaseModel):
    """Ask for the engine's current top-level status.

    No arguments. Returns per-symbol active/paused state, USD balance,
    session PnL, runtime elapsed.
    """

    kind: Literal["status"] = "status"

    class Config:
        frozen = True


class OpenOrdersQuery(BaseModel):
    """List open grid orders.

    ``symbol = None`` returns orders across every symbol.
    """

    kind: Literal["open_orders"] = "open_orders"
    symbol: OptionalSymbolInput = None

    class Config:
        frozen = True


class RecentFillsQuery(BaseModel):
    """List filled orders / cycles within a lookback window."""

    kind: Literal["recent_fills"] = "recent_fills"
    symbol: OptionalSymbolInput = None
    lookback_hours: int = Field(default=24, gt=0)
    limit: int = Field(default=20, gt=0, le=200)

    class Config:
        frozen = True


class RecentSuggestionsQuery(BaseModel):
    """List the most recent advisor suggestions from ``advise.db``."""

    kind: Literal["recent_suggestions"] = "recent_suggestions"
    symbol: OptionalSymbolInput = None
    limit: int = Field(default=5, gt=0, le=50)

    class Config:
        frozen = True


class RecentNewsQuery(BaseModel):
    """List recent news headlines from ``news.db``."""

    kind: Literal["recent_news"] = "recent_news"
    lookback_hours: int = Field(default=24, gt=0)
    limit: int = Field(default=10, gt=0, le=100)

    class Config:
        frozen = True


class HarvesterStatusQuery(BaseModel):
    """Ask the Harvester for current band classification + latest proposal."""

    kind: Literal["harvester_status"] = "harvester_status"

    class Config:
        frozen = True


class RecentProposalsQuery(BaseModel):
    """List recent harvester transfer proposals.

    ``direction = None`` returns both directions.
    """

    kind: Literal["recent_proposals"] = "recent_proposals"
    direction: Literal["exchange_to_bank", "bank_to_exchange"] | None = None
    lookback_hours: int = Field(default=24, gt=0)
    limit: int = Field(default=10, gt=0, le=100)

    class Config:
        frozen = True


class GridConfigQuery(BaseModel):
    """Return the grid params currently in effect.

    ``symbol = None`` returns the default-tier params; a concrete
    ``Symbol`` returns the per-symbol overrides if any.
    """

    kind: Literal["grid_config"] = "grid_config"
    symbol: OptionalSymbolInput = None

    class Config:
        frozen = True


class HelpQuery(BaseModel):
    """Return a structured listing of available commands and queries.

    Used by the assistant when the operator asks "what can you do?".
    """

    kind: Literal["help"] = "help"

    class Config:
        frozen = True


OperatorQuery = Annotated[
    StatusQuery
    | OpenOrdersQuery
    | RecentFillsQuery
    | RecentSuggestionsQuery
    | RecentNewsQuery
    | HarvesterStatusQuery
    | RecentProposalsQuery
    | GridConfigQuery
    | HelpQuery,
    Field(discriminator="kind"),
]
"""Discriminated union over all v1 read-only operator queries."""


# --------------------------------------------------------------------- #
# OperatorIntent — the LLM's outermost output                           #
# --------------------------------------------------------------------- #


class IntentCommand(BaseModel):
    """Operator's message resolved to a state-mutating command."""

    kind: Literal["command"] = "command"
    command: OperatorCommand

    class Config:
        frozen = True


class IntentQuery(BaseModel):
    """Operator's message resolved to a read-only query."""

    kind: Literal["query"] = "query"
    query: OperatorQuery

    class Config:
        frozen = True


class IntentConversational(BaseModel):
    """Operator's message is chat that does not resolve to an action.

    ``reply_text`` is what the bot should post back. Examples: "thanks",
    "lol", "what can you do?" (the assistant may answer help inline
    rather than emit a ``HelpQuery``).
    """

    kind: Literal["conversational"] = "conversational"
    reply_text: str = Field(min_length=1)

    class Config:
        frozen = True


class IntentUnparseable(BaseModel):
    """The assistant could not resolve the operator's message.

    ``reason`` is a short operator-facing explanation the bot will
    surface so the operator can rephrase.
    """

    kind: Literal["unparseable"] = "unparseable"
    reason: str = Field(min_length=1)

    class Config:
        frozen = True


OperatorIntent = Annotated[
    IntentCommand | IntentQuery | IntentConversational | IntentUnparseable,
    Field(discriminator="kind"),
]
"""Top-level discriminated union the assistant emits per operator turn."""


# --------------------------------------------------------------------- #
# Per-query result types                                                #
# --------------------------------------------------------------------- #


class SymbolStatusEntry(BaseModel):
    """One row of ``StatusResult.symbols`` — per-symbol engine state."""

    symbol: str = Field(min_length=3)
    state: Literal["active", "paused"]
    open_order_count: int = Field(ge=0)

    class Config:
        frozen = True


class StatusResult(BaseModel):
    """Top-level engine status."""

    kind: Literal["status"] = "status"
    symbols: list[SymbolStatusEntry] = Field(default_factory=list)
    total_usd_balance: float
    session_pnl: float
    session_runtime_seconds: float = Field(ge=0)
    recent_fill_count: int = Field(ge=0)

    class Config:
        frozen = True


class OpenOrderEntry(BaseModel):
    """One row of ``OpenOrdersResult.orders``."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=3)
    side: Literal["buy", "sell"]
    price: float = Field(gt=0)
    amount: float = Field(gt=0)
    created_at: Timestamp

    class Config:
        frozen = True


class OpenOrdersResult(BaseModel):
    """Listing of open grid orders."""

    kind: Literal["open_orders"] = "open_orders"
    symbol: str | None = None
    orders: list[OpenOrderEntry] = Field(default_factory=list)

    class Config:
        frozen = True


class FillEntry(BaseModel):
    """One row of ``RecentFillsResult.fills``."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=3)
    side: Literal["buy", "sell"]
    price: float = Field(gt=0)
    amount: float = Field(gt=0)
    pnl: float | None = None
    filled_at: Timestamp

    class Config:
        frozen = True


class RecentFillsResult(BaseModel):
    """Listing of recently filled orders / cycles."""

    kind: Literal["recent_fills"] = "recent_fills"
    symbol: str | None = None
    lookback_hours: int = Field(gt=0)
    fills: list[FillEntry] = Field(default_factory=list)

    class Config:
        frozen = True


class SuggestionEntry(BaseModel):
    """One row of ``RecentSuggestionsResult.suggestions``."""

    recommendation_id: str = Field(min_length=1)
    symbol: str = Field(min_length=3)
    model_name: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    recommendations: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1)
    created_at: Timestamp

    class Config:
        frozen = True


class RecentSuggestionsResult(BaseModel):
    """Listing of recent advisor suggestions."""

    kind: Literal["recent_suggestions"] = "recent_suggestions"
    symbol: str | None = None
    suggestions: list[SuggestionEntry] = Field(default_factory=list)

    class Config:
        frozen = True


class NewsEntry(BaseModel):
    """One row of ``RecentNewsResult.items``."""

    source: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    published_at: Timestamp
    sentiment_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    mentioned_coins: list[str] = Field(default_factory=list)

    class Config:
        frozen = True


class RecentNewsResult(BaseModel):
    """Listing of recent news items."""

    kind: Literal["recent_news"] = "recent_news"
    lookback_hours: int = Field(gt=0)
    items: list[NewsEntry] = Field(default_factory=list)

    class Config:
        frozen = True


class HarvesterStatusResult(BaseModel):
    """Current harvester band classification + latest proposal summary."""

    kind: Literal["harvester_status"] = "harvester_status"
    enabled: bool
    asset: str = Field(min_length=1, max_length=10)
    current_balance: float = Field(ge=0)
    band: Literal["deficit", "topup", "hold", "surplus"]
    latest_proposal_id: str | None = None
    latest_proposal_amount: float | None = None
    latest_proposal_direction: Literal["exchange_to_bank", "bank_to_exchange"] | None = None

    class Config:
        frozen = True


class ProposalEntry(BaseModel):
    """One row of ``RecentProposalsResult.proposals``."""

    proposal_id: str = Field(min_length=1)
    direction: Literal["exchange_to_bank", "bank_to_exchange"]
    asset: str = Field(min_length=1, max_length=10)
    amount: float = Field(gt=0)
    rationale: str = Field(min_length=1)
    created_at: Timestamp

    class Config:
        frozen = True


class RecentProposalsResult(BaseModel):
    """Listing of recent harvester proposals."""

    kind: Literal["recent_proposals"] = "recent_proposals"
    direction: Literal["exchange_to_bank", "bank_to_exchange"] | None = None
    lookback_hours: int = Field(gt=0)
    proposals: list[ProposalEntry] = Field(default_factory=list)

    class Config:
        frozen = True


class GridConfigResult(BaseModel):
    """Grid parameters currently in effect.

    ``symbol = None`` indicates default-tier params; a concrete symbol
    indicates per-symbol overrides applied on top.
    """

    kind: Literal["grid_config"] = "grid_config"
    symbol: str | None = None
    spacing_percentage: float = Field(gt=0)
    levels_above: int = Field(ge=0)
    levels_below: int = Field(ge=0)
    order_size_usd: float = Field(gt=0)

    class Config:
        frozen = True


class HelpEntry(BaseModel):
    """One row of ``HelpResult.entries`` — one command or query."""

    kind: str = Field(min_length=1)
    category: Literal["command", "query"]
    description: str = Field(min_length=1)

    class Config:
        frozen = True


class HelpResult(BaseModel):
    """Listing of available commands and queries with one-line descriptions."""

    kind: Literal["help"] = "help"
    entries: list[HelpEntry] = Field(default_factory=list)

    class Config:
        frozen = True


QueryResult = Annotated[
    StatusResult
    | OpenOrdersResult
    | RecentFillsResult
    | RecentSuggestionsResult
    | RecentNewsResult
    | HarvesterStatusResult
    | RecentProposalsResult
    | GridConfigResult
    | HelpResult,
    Field(discriminator="kind"),
]
"""Discriminated union over the typed outputs of every query."""


# --------------------------------------------------------------------- #
# CommandResult — what dispatching a command returns                    #
# --------------------------------------------------------------------- #


class CommandResult(BaseModel):
    """Outcome of dispatching an approved ``OperatorCommand``.

    ``success = False`` indicates the engine refused the command at the
    domain level (e.g. ``ResumeCommand`` for an already-active symbol).
    Protocol failures raise ``OperatorError`` instead of producing a
    failure ``CommandResult``.

    ``side_effects`` is a free-form dict — e.g. ``{"orders_cancelled": 4}``
    for ``CancelOpenOrdersCommand``. The dispatcher in Stage 5.4 fills
    this in.
    """

    success: bool
    command_kind: str = Field(min_length=1)
    message: str = Field(min_length=1)
    executed_at: Timestamp
    side_effects: dict[str, Any] = Field(default_factory=dict)

    class Config:
        frozen = True


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
    async def answer_query(self, query: OperatorQuery) -> QueryResult:
        """Answer a read-only operator query against current state.

        Args:
            query: Concrete ``OperatorQuery`` variant to answer.

        Returns:
            ``QueryResult`` variant matching the query's ``kind``.

        Raises:
            OperatorError: Storage unreachable or other infrastructure
                failure. Empty result sets are encoded in the result
                type's list fields, not raised.
        """

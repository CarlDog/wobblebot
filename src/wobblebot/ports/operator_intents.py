"""Operator-intent typed sums (Stage 8.0.A — Phase-5-audit refactor R5).

Split out of ``ports/operator.py`` for code organization. Houses the
three discriminated unions the LLM assistant emits + the variants
each union covers:

- :data:`OperatorCommand` — state-mutating actions
  (:class:`PauseCommand` | :class:`ResumeCommand` |
  :class:`PauseAllCommand` | :class:`ResumeAllCommand` |
  :class:`CancelOpenOrdersCommand` | :class:`StopCommand`).
- :data:`OperatorQuery` — read-only state questions
  (:class:`StatusQuery` ... :class:`HelpQuery`, 9 variants).
- :data:`OperatorIntent` — the outermost union the assistant emits
  (:class:`IntentCommand` | :class:`IntentQuery` |
  :class:`IntentConversational` | :class:`IntentUnparseable`).

Every variant has a ``kind: Literal[...]`` discriminator; every
variant is a frozen Pydantic model.

Per ADR-002 the LLM cannot execute. Commands only reach the engine
after the operator approves the corresponding ``PendingCommand``
row in operator.db via the Stage 5.4 confirm-before-execute gate.
This module defines the types; the gate logic lives in
``services/operator_service.py`` and ``cli/live.py``.

Public-API stability: every name in this module is re-exported by
``ports/operator.py`` for backward compatibility with the ~35
existing callsites that do
``from wobblebot.ports.operator import PauseCommand``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from wobblebot.domain.value_objects import Symbol

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
"""Discriminated union over all v1 state-mutating operator commands."""


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


class StatusReportQuery(BaseModel):
    """Aggregate snapshot across every query + LLM-condensed prose.

    Used by the assistant when the operator asks "give me a status
    report" / "what's new since last brief". ``lookback_hours = None``
    means "use the operator's last-status-report timestamp" (falls
    back to 24h on first invocation); an explicit value overrides
    the stored anchor and pins a fixed window.
    """

    kind: Literal["status_report"] = "status_report"
    lookback_hours: int | None = Field(default=None, gt=0, le=168)

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
    | HelpQuery
    | StatusReportQuery,
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
"""Discriminated union over what the assistant emits after parsing operator text."""


__all__ = (
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
    "StatusReportQuery",
    "StopCommand",
    "SymbolInput",
)

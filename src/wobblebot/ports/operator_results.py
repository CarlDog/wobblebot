"""Operator-query result types + CommandResult (Stage 8.0.A — R5).

Split out of ``ports/operator.py`` for code organization. Houses
every per-query result + entry type, the :data:`QueryResult`
discriminated union over them, and :class:`CommandResult` (the
output of dispatching an :class:`OperatorCommand`).

Each per-query result carries a ``kind: Literal[...]`` discriminator
matching the source query's ``kind`` — so ``StatusQuery`` is
answered with :class:`StatusResult`, ``OpenOrdersQuery`` with
:class:`OpenOrdersResult`, and so on. The 1-to-1 mapping keeps the
``OperatorService.answer_query`` dispatch table simple.

Public-API stability: every name in this module is re-exported by
``ports/operator.py`` for backward compatibility.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp

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


__all__ = (
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

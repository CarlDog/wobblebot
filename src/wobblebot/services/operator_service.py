"""OperatorService — Stage 5.4 ``OperatorPort`` implementation.

The engine-side of the operator interaction layer. ``cli/operator``
(Stage 5.6) writes ``PendingCommand`` rows; ``cli/live`` polls for
``status='approved'`` rows and routes each to
:meth:`OperatorService.dispatch_command`. Query rows route to
:meth:`OperatorService.answer_query`.

**ADR-002 firewall:** the *poll* enforces the approved-only invariant
— this service trusts what it gets. There is no code path from a
``PendingCommand`` to the engine that doesn't first see
``status == 'approved'`` at the cli/live polling layer (Stage 5.4.D).

Constructor takes a primary ``StoragePort`` for the engine's live.db
(open orders, recent trades, balance snapshots) plus optional
``advise_storage`` / ``news_storage`` / ``harvest_storage`` for the
cross-database queries (``RecentSuggestionsQuery``,
``RecentNewsQuery``, ``HarvesterStatusQuery``, ``RecentProposalsQuery``).
If those storages aren't wired, the corresponding queries return
empty result lists rather than raising — operators see "no data
available" instead of a crash.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from wobblebot.config.grid import GridConfig
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import OperatorError, StorageError
from wobblebot.ports.operator import (
    CancelOpenOrdersCommand,
    CommandResult,
    FillEntry,
    GridConfigQuery,
    GridConfigResult,
    HarvesterStatusQuery,
    HarvesterStatusResult,
    HelpEntry,
    HelpQuery,
    HelpResult,
    NewsEntry,
    OpenOrderEntry,
    OpenOrdersQuery,
    OpenOrdersResult,
    OperatorCommand,
    OperatorPort,
    OperatorQuery,
    PauseAllCommand,
    PauseCommand,
    ProposalEntry,
    QueryResult,
    RecentFillsQuery,
    RecentFillsResult,
    RecentNewsQuery,
    RecentNewsResult,
    RecentProposalsQuery,
    RecentProposalsResult,
    RecentSuggestionsQuery,
    RecentSuggestionsResult,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StatusResult,
    StopCommand,
    SuggestionEntry,
    SymbolStatusEntry,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.services.grid_engine import GridEngine

# Static help payload — assembled once and reused. Keep in sync with the
# command + query catalogs in ports/operator.py and the operator prompt
# (config/prompts/operator.md). Test asserts the kind set matches.
_HELP_ENTRIES: tuple[HelpEntry, ...] = (
    # Commands
    HelpEntry(kind="pause", category="command", description="Pause one symbol's grid."),
    HelpEntry(kind="resume", category="command", description="Resume one paused symbol's grid."),
    HelpEntry(kind="pause_all", category="command", description="Pause every active symbol."),
    HelpEntry(kind="resume_all", category="command", description="Resume every paused symbol."),
    HelpEntry(
        kind="cancel_open_orders",
        category="command",
        description="Cancel open grid orders on a symbol (or all).",
    ),
    HelpEntry(
        kind="stop", category="command", description="Soft-stop the engine (clean shutdown)."
    ),
    # Queries
    HelpEntry(kind="status", category="query", description="Engine status, balance, runtime."),
    HelpEntry(kind="open_orders", category="query", description="Open grid orders."),
    HelpEntry(kind="recent_fills", category="query", description="Recent filled orders / cycles."),
    HelpEntry(
        kind="recent_suggestions", category="query", description="Last N advisor suggestions."
    ),
    HelpEntry(kind="recent_news", category="query", description="Recent ingested news headlines."),
    HelpEntry(
        kind="harvester_status", category="query", description="Harvester band + latest proposal."
    ),
    HelpEntry(
        kind="recent_proposals",
        category="query",
        description="Recent harvester transfer proposals.",
    ),
    HelpEntry(kind="grid_config", category="query", description="Current grid params in effect."),
    HelpEntry(kind="help", category="query", description="List available commands and queries."),
)


class OperatorService(OperatorPort):  # pylint: disable=too-many-instance-attributes
    """Concrete ``OperatorPort`` wired to ``GridEngine`` + storage.

    All command dispatch + query answering converges through the two
    ``OperatorPort`` methods. Domain-level refusals (e.g. resuming an
    already-active symbol, listing fills on a symbol the engine isn't
    trading) come back as structured ``CommandResult.success=False`` /
    empty-list ``*Result`` rather than exceptions; protocol failures
    (storage unreachable) raise ``OperatorError`` per the port's
    contract.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        engine: GridEngine,
        storage: StoragePort,
        active_symbols: tuple[Symbol, ...] = (),
        grid_config: GridConfig | None = None,
        advise_storage: StoragePort | None = None,
        news_storage: StoragePort | None = None,
        harvest_storage: StoragePort | None = None,
        harvester_config: HarvesterConfig | None = None,
        session_started_at: Timestamp | None = None,
    ) -> None:
        self._engine = engine
        self._storage = storage
        self._active_symbols = tuple(active_symbols)
        self._grid_config = grid_config
        self._advise_storage = advise_storage
        self._news_storage = news_storage
        self._harvest_storage = harvest_storage
        self._harvester_config = harvester_config
        self._session_started_at = session_started_at

    # ------------------------------------------------------------------ commands

    async def dispatch_command(self, command: OperatorCommand) -> CommandResult:
        match command:
            case PauseCommand():
                return self._dispatch_pause(command)
            case ResumeCommand():
                return self._dispatch_resume(command)
            case PauseAllCommand():
                return self._dispatch_pause_all()
            case ResumeAllCommand():
                return self._dispatch_resume_all()
            case CancelOpenOrdersCommand():
                return await self._dispatch_cancel_open_orders(command)
            case StopCommand():
                return self._dispatch_stop()
            case _:
                raise OperatorError(f"Unknown OperatorCommand variant: {type(command).__name__}")

    def _dispatch_pause(self, command: PauseCommand) -> CommandResult:
        changed = self._engine.pause_symbol(command.symbol)
        message = (
            f"Paused {command.symbol}." if changed else f"{command.symbol} was already paused."
        )
        return CommandResult(
            success=changed,
            command_kind="pause",
            message=message,
            executed_at=_now(),
            side_effects={"symbol": str(command.symbol), "newly_paused": changed},
        )

    def _dispatch_resume(self, command: ResumeCommand) -> CommandResult:
        changed = self._engine.resume_symbol(command.symbol)
        message = (
            f"Resumed {command.symbol}." if changed else f"{command.symbol} was already active."
        )
        return CommandResult(
            success=changed,
            command_kind="resume",
            message=message,
            executed_at=_now(),
            side_effects={"symbol": str(command.symbol), "newly_resumed": changed},
        )

    def _dispatch_pause_all(self) -> CommandResult:
        paused: list[str] = []
        for symbol in self._active_symbols:
            if self._engine.pause_symbol(symbol):
                paused.append(str(symbol))
        return CommandResult(
            success=bool(paused),
            command_kind="pause_all",
            message=(
                f"Paused {len(paused)} symbol(s): {', '.join(paused)}."
                if paused
                else "No active symbols to pause."
            ),
            executed_at=_now(),
            side_effects={"newly_paused": paused, "count": len(paused)},
        )

    def _dispatch_resume_all(self) -> CommandResult:
        resumed: list[str] = []
        for symbol in self._engine.paused_symbols():
            if self._engine.resume_symbol(symbol):
                resumed.append(str(symbol))
        return CommandResult(
            success=bool(resumed),
            command_kind="resume_all",
            message=(
                f"Resumed {len(resumed)} symbol(s): {', '.join(resumed)}."
                if resumed
                else "No paused symbols to resume."
            ),
            executed_at=_now(),
            side_effects={"newly_resumed": resumed, "count": len(resumed)},
        )

    async def _dispatch_cancel_open_orders(self, command: CancelOpenOrdersCommand) -> CommandResult:
        cancelled, failed = await self._engine.cancel_open_orders(symbol=command.symbol)
        scope = str(command.symbol) if command.symbol else "all symbols"
        return CommandResult(
            success=cancelled > 0 or failed == 0,
            command_kind="cancel_open_orders",
            message=f"Cancelled {cancelled} order(s) on {scope}; {failed} failed.",
            executed_at=_now(),
            side_effects={
                "scope": scope,
                "cancelled": cancelled,
                "failed": failed,
            },
        )

    def _dispatch_stop(self) -> CommandResult:
        already_requested = self._engine.is_stop_requested
        self._engine.request_stop()
        return CommandResult(
            success=not already_requested,
            command_kind="stop",
            message=(
                "Stop already requested."
                if already_requested
                else "Soft stop requested; cli/live will exit cleanly."
            ),
            executed_at=_now(),
            side_effects={"already_requested": already_requested},
        )

    # ------------------------------------------------------------------ queries

    async def answer_query(  # pylint: disable=too-many-return-statements
        self, query: OperatorQuery
    ) -> QueryResult:
        match query:
            case StatusQuery():
                return await self._answer_status()
            case OpenOrdersQuery():
                return await self._answer_open_orders(query)
            case RecentFillsQuery():
                return await self._answer_recent_fills(query)
            case RecentSuggestionsQuery():
                return await self._answer_recent_suggestions(query)
            case RecentNewsQuery():
                return await self._answer_recent_news(query)
            case HarvesterStatusQuery():
                return await self._answer_harvester_status()
            case RecentProposalsQuery():
                return await self._answer_recent_proposals(query)
            case GridConfigQuery():
                return self._answer_grid_config(query)
            case HelpQuery():
                return HelpResult(entries=list(_HELP_ENTRIES))
            case _:
                raise OperatorError(f"Unknown OperatorQuery variant: {type(query).__name__}")

    async def _answer_status(self) -> StatusResult:
        symbol_entries: list[SymbolStatusEntry] = []
        for symbol in self._active_symbols:
            try:
                opens = await self._storage.get_open_orders(symbol=symbol)
            except StorageError as exc:
                raise OperatorError(f"Failed to read open orders for {symbol}: {exc}") from exc
            symbol_entries.append(
                SymbolStatusEntry(
                    symbol=str(symbol),
                    state="paused" if self._engine.is_paused(symbol) else "active",
                    open_order_count=len(opens),
                )
            )

        # Session-PnL calculation is deferred to a later stage; v1 ships
        # zero so the operator's status reply doesn't lie. Recent_fills
        # query gives them the trade-level view.
        total_usd_balance = await self._fetch_total_usd_balance()
        runtime_seconds = self._runtime_seconds()
        recent_fill_count = await self._recent_fill_count()

        return StatusResult(
            symbols=symbol_entries,
            total_usd_balance=total_usd_balance,
            session_pnl=0.0,
            session_runtime_seconds=runtime_seconds,
            recent_fill_count=recent_fill_count,
        )

    async def _answer_open_orders(self, query: OpenOrdersQuery) -> OpenOrdersResult:
        try:
            opens = await self._storage.get_open_orders(symbol=query.symbol)
        except StorageError as exc:
            raise OperatorError(f"Failed to read open orders: {exc}") from exc
        entries = [
            OpenOrderEntry(
                order_id=str(o.id),
                symbol=str(o.symbol),
                side=o.side.value,
                price=float(o.price.amount),
                amount=float(o.amount.value),
                created_at=o.created_at,
            )
            for o in opens
        ]
        return OpenOrdersResult(
            symbol=str(query.symbol) if query.symbol else None,
            orders=entries,
        )

    async def _answer_recent_fills(self, query: RecentFillsQuery) -> RecentFillsResult:
        cutoff = datetime.now(UTC) - timedelta(hours=query.lookback_hours)
        try:
            trades = await self._storage.get_trades(
                symbol=query.symbol,
                start_time=cutoff,
                limit=query.limit,
            )
        except StorageError as exc:
            raise OperatorError(f"Failed to read trades: {exc}") from exc
        entries = [
            FillEntry(
                order_id=t.order_id,
                symbol=str(t.symbol),
                side=t.side.value,
                price=float(t.price.amount),
                amount=float(t.amount.value),
                pnl=None,  # cycle-level PnL is computed elsewhere; trade-level is just cost
                filled_at=t.executed_at,
            )
            for t in trades
        ]
        return RecentFillsResult(
            symbol=str(query.symbol) if query.symbol else None,
            lookback_hours=query.lookback_hours,
            fills=entries,
        )

    async def _answer_recent_suggestions(
        self, query: RecentSuggestionsQuery
    ) -> RecentSuggestionsResult:
        if self._advise_storage is None:
            return RecentSuggestionsResult(
                symbol=str(query.symbol) if query.symbol else None,
                suggestions=[],
            )
        try:
            rows = await self._advise_storage.get_advisor_suggestions(limit=query.limit)
        except StorageError as exc:
            raise OperatorError(f"Failed to read advisor suggestions: {exc}") from exc
        entries: list[SuggestionEntry] = []
        target_symbol = str(query.symbol) if query.symbol else None
        for row in rows:
            input_summary = row.input_summary if isinstance(row.input_summary, dict) else {}
            row_symbol = str(input_summary.get("symbol", "")) or "?"
            if target_symbol is not None and row_symbol != target_symbol:
                continue
            entries.append(
                SuggestionEntry(
                    recommendation_id=row.recommendation.recommendation_id,
                    symbol=row_symbol if row_symbol != "?" else "UNK/UNK",
                    model_name=row.model_name,
                    confidence=row.recommendation.confidence,
                    recommendations=row.recommendation.recommendations,
                    rationale=row.recommendation.rationale,
                    created_at=row.created_at,
                )
            )
        return RecentSuggestionsResult(
            symbol=target_symbol,
            suggestions=entries,
        )

    async def _answer_recent_news(self, query: RecentNewsQuery) -> RecentNewsResult:
        if self._news_storage is None:
            return RecentNewsResult(lookback_hours=query.lookback_hours, items=[])
        cutoff = datetime.now(UTC) - timedelta(hours=query.lookback_hours)
        try:
            items = await self._news_storage.get_news_items(since=cutoff, limit=query.limit)
        except StorageError as exc:
            raise OperatorError(f"Failed to read news items: {exc}") from exc
        entries = [
            NewsEntry(
                source=item.source,
                headline=item.headline,
                published_at=item.published_at,
                sentiment_score=item.sentiment_score,
                mentioned_coins=list(item.mentioned_coins),
            )
            for item in items
        ]
        return RecentNewsResult(lookback_hours=query.lookback_hours, items=entries)

    async def _answer_harvester_status(self) -> HarvesterStatusResult:
        enabled = self._harvester_config.enabled if self._harvester_config else False
        if self._harvest_storage is None or self._harvester_config is None:
            # No harvester wired — return a graceful "deficit, nothing pending" stub.
            balances = await self._fetch_total_usd_balance()
            return HarvesterStatusResult(
                enabled=enabled,
                asset="USD",
                current_balance=balances,
                band=_classify_band(Decimal(str(balances)), self._harvester_config),
            )
        try:
            proposals = await self._harvest_storage.get_transfer_proposals(limit=1)
        except StorageError as exc:
            raise OperatorError(f"Failed to read transfer proposals: {exc}") from exc
        latest = proposals[0] if proposals else None
        balance = await self._fetch_total_usd_balance()
        return HarvesterStatusResult(
            enabled=enabled,
            asset="USD",
            current_balance=balance,
            band=_classify_band(Decimal(str(balance)), self._harvester_config),
            latest_proposal_id=latest.proposal_id if latest else None,
            latest_proposal_amount=float(latest.amount) if latest else None,
            latest_proposal_direction=latest.direction if latest else None,
        )

    async def _answer_recent_proposals(self, query: RecentProposalsQuery) -> RecentProposalsResult:
        if self._harvest_storage is None:
            return RecentProposalsResult(
                direction=query.direction,
                lookback_hours=query.lookback_hours,
                proposals=[],
            )
        cutoff = datetime.now(UTC) - timedelta(hours=query.lookback_hours)
        try:
            proposals = await self._harvest_storage.get_transfer_proposals(
                since=cutoff,
                direction=query.direction,
                limit=query.limit,
            )
        except StorageError as exc:
            raise OperatorError(f"Failed to read transfer proposals: {exc}") from exc
        entries = [
            ProposalEntry(
                proposal_id=p.proposal_id,
                direction=p.direction,
                asset=p.asset,
                amount=float(p.amount),
                rationale=p.rationale,
                created_at=p.created_at,
            )
            for p in proposals
        ]
        return RecentProposalsResult(
            direction=query.direction,
            lookback_hours=query.lookback_hours,
            proposals=entries,
        )

    def _answer_grid_config(self, query: GridConfigQuery) -> GridConfigResult:
        if self._grid_config is None:
            # No grid config wired — return defaults via Pydantic (zero
            # placeholders would fail validation, so we use small positives).
            return GridConfigResult(
                symbol=str(query.symbol) if query.symbol else None,
                spacing_percentage=1.0,
                levels_above=0,
                levels_below=0,
                order_size_usd=1.0,
            )
        # for_coin resolves per-coin overrides on top of the default tier.
        # When query.symbol is None, fall back to the default tier directly.
        if query.symbol is None:
            tier = self._grid_config.default
            return GridConfigResult(
                symbol=None,
                spacing_percentage=float(tier.spacing_percentage),
                levels_above=tier.levels_above,
                levels_below=tier.levels_below,
                order_size_usd=float(tier.order_size_usd),
            )
        coin = self._grid_config.for_coin(query.symbol.base)
        return GridConfigResult(
            symbol=str(query.symbol),
            spacing_percentage=float(coin.spacing_percentage),
            levels_above=coin.levels_above,
            levels_below=coin.levels_below,
            order_size_usd=float(coin.order_size_usd),
        )

    # ------------------------------------------------------------------ helpers

    async def _fetch_total_usd_balance(self) -> float:
        try:
            balances = await self._storage.get_latest_balance_snapshot()
        except StorageError as exc:
            raise OperatorError(f"Failed to read latest balance snapshot: {exc}") from exc
        for entry in balances:
            if entry.asset.upper() == "USD":
                return float(entry.total)
        return 0.0

    def _runtime_seconds(self) -> float:
        if self._session_started_at is None:
            return 0.0
        delta = datetime.now(UTC) - self._session_started_at.dt
        return max(0.0, delta.total_seconds())

    async def _recent_fill_count(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        try:
            trades = await self._storage.get_trades(start_time=cutoff, limit=200)
        except StorageError as exc:
            raise OperatorError(f"Failed to count recent trades: {exc}") from exc
        return len(trades)


# --------------------------------------------------------------------- #
# Module helpers                                                         #
# --------------------------------------------------------------------- #


def _now() -> Timestamp:
    return Timestamp(dt=datetime.now(UTC))


def _classify_band(
    balance: Decimal, cfg: HarvesterConfig | None
) -> Any:  # actually Literal but Python literals don't widen
    """Classify a USD balance into the four harvester bands.

    Returns the same literal strings the ``HarvesterStatusResult.band``
    field expects (``deficit`` / ``topup`` / ``hold`` / ``surplus``).
    Falls back to ``deficit`` when no config is provided so the result
    type is always valid.
    """
    if cfg is None:
        return "deficit"
    if balance < cfg.min_exchange_liquidity_usd:
        return "deficit"
    if balance < cfg.topup_threshold_usd:
        return "topup"
    if balance < cfg.surplus_threshold_usd:
        return "hold"
    return "surplus"

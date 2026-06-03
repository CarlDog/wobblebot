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
from wobblebot.ports.assistant import AssistantPort
from wobblebot.ports.exceptions import (
    AssistantError,
    ExchangeError,
    OperatorError,
    StorageError,
)
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
    StatusReportQuery,
    StatusReportResult,
    StatusReportTally,
    StatusResult,
    StopCommand,
    SuggestionEntry,
    SymbolStatusEntry,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cycle_matcher import match_cycles, today_realized_pnl
from wobblebot.services.discord_embed_render import format_signed_usd
from wobblebot.services.grid_engine import GridEngine

# --------------------------------------------------------------------- #
# Degraded-result factories (Stage 8.0.B — R3)                          #
# --------------------------------------------------------------------- #
#
# When a query's cross-DB storage isn't wired (cli/operator started
# without that DB configured), return one of these factories' output
# from the query handler. Centralizing the "what does empty look like"
# knowledge in one place — adding a new graceful-degrade is a single
# edit here plus the call-site guard.
#
# Each factory takes the source query so it can echo back the query's
# filter knobs (symbol, lookback_hours, etc.) in the empty result,
# letting the operator see "0 results FOR THIS FILTER" instead of a
# context-free empty payload. Per the ADR-013 / Stage 5.6.C
# graceful-degrade contract, empty list is success, not failure —
# the operator decides whether "no results" is informative.
#
# HarvesterStatusQuery's degraded shape is genuinely different (it
# still fetches the live balance and classifies a band) and stays
# inline in ``_answer_harvester_status``; the factories below cover
# the three simple-shape cases.


def _empty_recent_suggestions(query: RecentSuggestionsQuery) -> RecentSuggestionsResult:
    """Degraded result when advise.db isn't wired."""
    return RecentSuggestionsResult(
        symbol=str(query.symbol) if query.symbol else None,
        suggestions=[],
    )


def _empty_recent_news(query: RecentNewsQuery) -> RecentNewsResult:
    """Degraded result when news.db isn't wired."""
    return RecentNewsResult(lookback_hours=query.lookback_hours, items=[])


def _empty_recent_proposals(query: RecentProposalsQuery) -> RecentProposalsResult:
    """Degraded result when harvest.db isn't wired."""
    return RecentProposalsResult(
        direction=query.direction,
        lookback_hours=query.lookback_hours,
        proposals=[],
    )


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
    HelpEntry(
        kind="status_report",
        category="query",
        description="Aggregated activity snapshot + LLM-condensed prose (since last brief).",
    ),
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
        observe_storage: StoragePort | None = None,
        operator_storage: StoragePort | None = None,
        harvester_config: HarvesterConfig | None = None,
        assistant: AssistantPort | None = None,
        session_started_at: Timestamp | None = None,
    ) -> None:
        self._engine = engine
        self._storage = storage
        self._active_symbols = tuple(active_symbols)
        self._grid_config = grid_config
        self._advise_storage = advise_storage
        self._news_storage = news_storage
        self._harvest_storage = harvest_storage
        self._observe_storage = observe_storage
        # ``operator_storage`` backs the ``status_report_history`` anchor
        # used by StatusReportQuery's "since last" lookback. Distinct
        # from ``storage`` (live.db) because the anchor table lives in
        # operator.db alongside pending_commands and conversation_turns.
        self._operator_storage = operator_storage
        self._harvester_config = harvester_config
        # AssistantPort drives the prose narrative on StatusReportQuery.
        # Optional — if None, the query falls back to a deterministic
        # one-line summary so it still works without an LLM.
        self._assistant = assistant
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
        scope = str(command.symbol) if command.symbol else "all symbols"
        try:
            cancelled, failed = await self._engine.cancel_open_orders(symbol=command.symbol)
        except ExchangeError as exc:
            # The open-order fetch failed, so we cannot know what (if anything)
            # is still live on Kraken. Report the uncertainty rather than a
            # false "0 cancelled, 0 failed" all-clear — the orders may persist.
            return CommandResult(
                success=False,
                command_kind="cancel_open_orders",
                message=(
                    f"Could not fetch open orders on {scope} ({exc}); "
                    "they may still be LIVE on Kraken. Retry shortly."
                ),
                executed_at=_now(),
                side_effects={"scope": scope, "fetch_failed": True, "error": str(exc)},
            )
        return CommandResult(
            # Any per-order cancel failure makes this not a clean all-clear:
            # the operator needs to retry, so don't flag it green.
            success=failed == 0,
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
        self,
        query: OperatorQuery,
        *,
        channel_id: str | None = None,
        user_id: str | None = None,
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
            case StatusReportQuery():
                return await self._answer_status_report(
                    query, channel_id=channel_id, user_id=user_id
                )
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

        total_usd_balance = await self._fetch_total_usd_balance()
        runtime_seconds = self._runtime_seconds()
        recent_fill_count = await self._recent_fill_count()
        session_pnl = await self._fetch_today_realized_pnl()

        return StatusResult(
            symbols=symbol_entries,
            total_usd_balance=total_usd_balance,
            session_pnl=session_pnl,
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
            return _empty_recent_suggestions(query)
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
            return _empty_recent_news(query)
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
            return _empty_recent_proposals(query)
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

    async def _answer_status_report(  # pylint: disable=too-many-locals
        self,
        query: StatusReportQuery,
        *,
        channel_id: str | None,
        user_id: str | None,
    ) -> StatusReportResult:
        """Aggregate every read-only query + condense via LLM."""
        now = datetime.now(UTC)
        lookback_hours, since = await self._resolve_status_report_window(
            query, channel_id=channel_id, user_id=user_id, now=now
        )

        # Run the sub-queries we want to roll up. Failures degrade
        # gracefully — we still produce a report with the sections that
        # succeeded so a single broken cross-DB doesn't kill the brief.
        status = await self._answer_status()
        open_orders = await self._answer_open_orders(OpenOrdersQuery(symbol=None))
        recent_fills = await self._answer_recent_fills(
            RecentFillsQuery(symbol=None, lookback_hours=lookback_hours, limit=20)
        )
        # RecentSuggestionsQuery has no lookback_hours parameter -- the
        # query returns the most-recent N suggestions regardless of when
        # they were made. For status_report we need them lookback-scoped
        # like fills and news, so we widen the limit and filter
        # post-query by created_at.
        suggestions_raw = await self._answer_recent_suggestions(
            RecentSuggestionsQuery(symbol=None, limit=50)
        )
        lookback_cutoff = now - timedelta(hours=lookback_hours)
        recent_suggestions = suggestions_raw.model_copy(
            update={
                "suggestions": [
                    s for s in suggestions_raw.suggestions if s.created_at.dt > lookback_cutoff
                ]
            }
        )
        recent_news = await self._answer_recent_news(
            RecentNewsQuery(lookback_hours=lookback_hours, limit=10)
        )
        harvester_status = await self._answer_harvester_status()
        recent_proposals = await self._answer_recent_proposals(
            RecentProposalsQuery(direction=None, lookback_hours=lookback_hours, limit=5)
        )
        # Include default-tier grid config in the data blob so the LLM
        # can ground "spacing is currently X" claims against advisor
        # suggestions like "increase spacing to 1.2".
        grid_config = self._answer_grid_config(GridConfigQuery(symbol=None))

        tallies = [
            StatusReportTally(label="Balance", value=f"${status.total_usd_balance:,.2f}"),
            StatusReportTally(
                label="Today's PnL", value=format_signed_usd(status.session_pnl, decimals=4)
            ),
            StatusReportTally(label="Open orders", value=str(len(open_orders.orders))),
            StatusReportTally(
                label=f"Fills (last {lookback_hours}h)", value=str(len(recent_fills.fills))
            ),
            StatusReportTally(
                label=f"News (last {lookback_hours}h)", value=str(len(recent_news.items))
            ),
            StatusReportTally(
                label=f"Suggestions (last {lookback_hours}h)",
                value=str(len(recent_suggestions.suggestions)),
            ),
            StatusReportTally(label="Harvester band", value=harvester_status.band),
            StatusReportTally(label="Proposals", value=str(len(recent_proposals.proposals))),
        ]

        narrative = await self._compose_status_report_narrative(
            lookback_hours=lookback_hours,
            status=status,
            open_orders=open_orders,
            recent_fills=recent_fills,
            recent_suggestions=recent_suggestions,
            recent_news=recent_news,
            harvester_status=harvester_status,
            recent_proposals=recent_proposals,
            grid_config=grid_config,
        )

        # Persist the anchor so the next status_report can scope its
        # window. Persist AFTER the narrative compose so a transient LLM
        # failure doesn't move the anchor and lose the data window.
        if self._operator_storage is not None and channel_id is not None and user_id is not None:
            try:
                await self._operator_storage.save_status_report_taken(channel_id, user_id, now)
            except StorageError:
                # Anchor save failure is non-fatal — operator still sees
                # the report. Next run just won't have an updated anchor.
                pass

        return StatusReportResult(
            lookback_hours=lookback_hours,
            since=Timestamp(dt=since),
            narrative=narrative,
            tallies=tallies,
        )

    async def _resolve_status_report_window(
        self,
        query: StatusReportQuery,
        *,
        channel_id: str | None,
        user_id: str | None,
        now: datetime,
    ) -> tuple[int, datetime]:
        """Pick the lookback window: explicit override > stored anchor > 24h."""
        if query.lookback_hours is not None:
            hours = query.lookback_hours
            return hours, now - timedelta(hours=hours)
        if self._operator_storage is not None and channel_id is not None and user_id is not None:
            try:
                anchor = await self._operator_storage.get_last_status_report_taken_at(
                    channel_id, user_id
                )
            except StorageError:
                anchor = None
            if anchor is not None:
                # Compute hours since anchor, round up to at least 1.
                delta = now - anchor
                hours = max(1, int(delta.total_seconds() // 3600))
                return hours, anchor
        return 24, now - timedelta(hours=24)

    async def _compose_status_report_narrative(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        lookback_hours: int,
        status: StatusResult,
        open_orders: OpenOrdersResult,
        recent_fills: RecentFillsResult,
        recent_suggestions: RecentSuggestionsResult,
        recent_news: RecentNewsResult,
        harvester_status: HarvesterStatusResult,
        recent_proposals: RecentProposalsResult,
        grid_config: GridConfigResult,
    ) -> str:
        """Build the LLM prompt + call summarize; fall back deterministically."""
        deterministic = (
            f"Last {lookback_hours}h snapshot: balance ${status.total_usd_balance:,.2f}, "
            f"today's PnL {format_signed_usd(status.session_pnl, decimals=4)}, "
            f"{len(recent_fills.fills)} fills, {len(recent_news.items)} news items, "
            f"harvester band {harvester_status.band}, "
            f"{len(open_orders.orders)} open orders."
        )
        if self._assistant is None:
            return deterministic

        # Compact data blob. The COUNTS section is pre-computed and
        # authoritative — every count in the narrative MUST come from
        # this section, not from re-counting JSON arrays the LLM is
        # prone to miscount. This block addresses 2026-05-24 audit
        # finding #4: phi4 was conflating ``STATUS.recent_fill_count``
        # (engine-wide tally) with the lookback-scoped fill count from
        # RECENT_FILLS, and miscounting open_orders sides (saying
        # "five buy orders" when 5 was the total open-order count).
        open_buys = sum(1 for o in open_orders.orders if o.side == "buy")
        open_sells = sum(1 for o in open_orders.orders if o.side == "sell")
        fills_buys = sum(1 for f in recent_fills.fills if f.side == "buy")
        fills_sells = sum(1 for f in recent_fills.fills if f.side == "sell")
        counts_block = [
            f"  lookback_window_hours: {lookback_hours}",
            f"  open_orders_total: {len(open_orders.orders)}",
            f"  open_buys: {open_buys}",
            f"  open_sells: {open_sells}",
            f"  fills_in_lookback_total: {len(recent_fills.fills)}",
            f"  fills_in_lookback_buys: {fills_buys}",
            f"  fills_in_lookback_sells: {fills_sells}",
            f"  news_in_lookback: {len(recent_news.items)}",
            f"  suggestions_in_lookback: {len(recent_suggestions.suggestions)}",
            f"  proposals_in_lookback: {len(recent_proposals.proposals)}",
            f"  harvester_band: {harvester_status.band}",
            f"  total_usd_balance: {status.total_usd_balance:,.2f}",
            f"  todays_realized_pnl: " f"{format_signed_usd(status.session_pnl, decimals=4)}",
        ]
        blob_lines = [
            f"LOOKBACK_HOURS: {lookback_hours}",
            "",
            "COUNTS (authoritative -- cite these verbatim; never re-count):",
            *counts_block,
            "",
            "STATUS (engine-wide; recent_fill_count here is NOT lookback-scoped):",
            status.model_dump_json(indent=2),
            "",
            "GRID_CONFIG (currently in effect):",
            grid_config.model_dump_json(indent=2),
            "",
            "OPEN_ORDERS:",
            open_orders.model_dump_json(indent=2),
            "",
            "RECENT_FILLS (only the lookback window):",
            recent_fills.model_dump_json(indent=2),
            "",
            "RECENT_SUGGESTIONS (only the lookback window; proposed changes, not yet applied):",
            recent_suggestions.model_dump_json(indent=2),
            "",
            "RECENT_NEWS:",
            recent_news.model_dump_json(indent=2),
            "",
            "HARVESTER_STATUS:",
            harvester_status.model_dump_json(indent=2),
            "",
            "RECENT_PROPOSALS:",
            recent_proposals.model_dump_json(indent=2),
        ]
        user_content = "\n".join(blob_lines)

        system_prompt = (
            "You are the WobbleBot operator assistant generating a status "
            "report. The operator has asked for a snapshot of what's "
            "happened since they last checked. You will receive structured "
            "JSON for every query the bot can answer; condense it into a "
            "user-friendly 2-3 paragraph plain-text narrative.\n\n"
            "**The COUNTS section is authoritative.** Every count you "
            "mention in the narrative (fills, open orders by side, news "
            "items, suggestions, proposals) MUST come from COUNTS "
            "verbatim. Do NOT re-count by inspecting JSON arrays in "
            "other sections. Do NOT use STATUS.recent_fill_count for "
            "fill counts -- that is engine-wide, not lookback-scoped. "
            "Every COUNTS field ending in ``_in_lookback`` is scoped to "
            "the requested window.\n\n"
            "**If a `_in_lookback` count is 0, say so explicitly** and "
            "do not invent activity. Examples:\n"
            "  - fills_in_lookback_total=0 -> 'no fills in the lookback window'\n"
            "  - news_in_lookback=0 -> 'no news in the lookback window'\n"
            "  - suggestions_in_lookback=0 -> 'no new advisor suggestions in "
            "the lookback window' (existing advice from earlier still "
            "applies; just don't pretend new ones arrived)\n"
            "  - proposals_in_lookback=0 -> 'no harvester proposals in the "
            "lookback window'\n\n"
            "Guidelines:\n"
            "- Lead with what changed (fills, new news, harvester movements). "
            "Static state (open orders, grid config) is secondary.\n"
            "- When discussing RECENT_SUGGESTIONS, compare proposed values "
            "against GRID_CONFIG (e.g. 'advisor recommends bumping spacing "
            "from 1.0% to 1.2%') -- don't describe suggestions in isolation.\n"
            "- Surface prices and timestamps that matter. Don't invent "
            "numbers not in the JSON.\n"
            "- If a section is empty, say so briefly; don't pad.\n"
            "- Use Markdown sparingly -- bold for headlines, plain text for the "
            "rest. No code fences, no JSON in the output.\n"
            "- Keep it under ~300 words. The operator wants signal, not noise."
        )

        try:
            narrative = await self._assistant.summarize(
                system_prompt, user_content, max_tokens=2048
            )
        except (AssistantError, NotImplementedError):
            return deterministic
        return narrative or deterministic

    # ------------------------------------------------------------------ helpers

    async def _fetch_total_usd_balance(self) -> float:
        """Latest USD balance from observe.db's balance_snapshots.

        Stage 5.6's original wiring queried ``self._storage`` (live.db)
        for balance snapshots — but balance snapshots only live in
        observe.db (cli/observe's optional balance-polling cadence).
        After the 2026-05-24 Discord-visibility fix, the OperatorService
        accepts an optional ``observe_storage`` and prefers it for this
        lookup. Falls back to ``self._storage`` for backward compat;
        returns 0.0 if no USD entry exists in either.
        """
        balance_storage = self._observe_storage or self._storage
        try:
            balances = await balance_storage.get_latest_balance_snapshot()
        except StorageError as exc:
            raise OperatorError(f"Failed to read latest balance snapshot: {exc}") from exc
        for entry in balances:
            if entry.asset.upper() == "USD":
                return float(entry.total)
        return 0.0

    async def _fetch_today_realized_pnl(self) -> float:
        """Today's realized PnL from completed BUY→SELL cycles in live.db.

        Pre-2026-05-24 the StatusResult shipped a hardcoded ``session_pnl=0.0``
        with a comment "deferred to a later stage." Now ``cycle_matcher``
        is available; the OperatorService computes today's realized PnL
        the same way the dashboard does. Uses UTC day boundary by default
        — the Discord bot has no operator-tz context the way the web UI
        does (web UI threads ``prefs.timezone`` from the auth session;
        Discord has no equivalent user-preference layer in v1).
        """
        try:
            trades = await self._storage.get_trades(limit=100)
        except StorageError as exc:
            raise OperatorError(f"Failed to read recent trades for PnL: {exc}") from exc
        if not trades:
            return 0.0
        cycles = match_cycles(trades)
        if not cycles:
            return 0.0
        return float(today_realized_pnl(cycles))

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

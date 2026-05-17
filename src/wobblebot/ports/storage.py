"""StoragePort - Abstract interface for persistence operations.

This port defines the contract for storing and retrieving domain entities.
Adapters implement this interface for specific storage backends (SQLite, Postgres, etc.).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Balance, NewsItem, Order, PriceSnapshot, Trade
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorSuggestion, AppliedSuggestion
from wobblebot.ports.assistant import ConversationTurn
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.notifier import Notification, PersistedNotification
from wobblebot.ports.operator import PendingCommand, PendingCommandStatus


class StoragePort(ABC):  # pylint: disable=too-many-public-methods
    """Abstract interface for persistence operations.

    Implementations must handle:
    - Order persistence and queries
    - Trade history
    - Balance snapshots
    - Configuration snapshots (future)
    - Audit logs (future)

    Position tracking deferred to Phase 3+ (see ADR-005).

    Error convention:
    - Domain-data miss returns ``None`` (e.g. unknown order id).
      Empty list queries return ``[]``.
    - Protocol failure raises ``StorageError`` (DB unreachable,
      constraint violation that domain layer cannot prevent, etc.).

    Caller contract for concurrent writes:
    - The adapter offers no optimistic concurrency control. Two
      coroutines doing ``get_order(X) -> mutate -> save_order(X)``
      concurrently will clobber each other silently. Callers (Bot
      Core in Phase 2+) MUST serialize per-entity writes themselves —
      e.g. a per-order ``asyncio.Lock`` keyed by ``order.id``.
    """

    # Order operations
    @abstractmethod
    async def save_order(self, order: Order) -> None:
        """Persist an order.

        Args:
            order: Order to save (insert or update)

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_order(self, order_id: UUID) -> Order | None:
        """Retrieve an order by ID.

        Args:
            order_id: Order ID

        Returns:
            Order if found, None otherwise

        Raises:
            StorageError: If retrieval fails
        """
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        """Get all open orders, optionally filtered by symbol.

        Args:
            symbol: Optional symbol filter

        Returns:
            List of open orders

        Raises:
            StorageError: If retrieval fails
        """
        pass

    @abstractmethod
    async def get_orders(
        self,
        symbol: Symbol | None = None,
        side: str | None = None,
        created_after: datetime | None = None,
    ) -> list[Order]:
        """Query orders by symbol / side / creation time. No status filter —
        returns orders in any status.

        Used by the safety cap layer (Stage 2.2.4) to compute committed
        daily-spend across all order outcomes (open, closed, canceled).
        Per-coin and total exposure caps use ``get_open_orders`` instead.

        Args:
            symbol: Optional symbol filter.
            side: Optional ``"buy"`` or ``"sell"`` filter.
            created_after: Optional lower bound on ``created_at`` (UTC,
                tz-aware required).

        Returns:
            Matching orders. Empty list if none match. ORDER BY created_at.

        Raises:
            StorageError: If retrieval fails.
        """
        pass

    # Trade operations
    @abstractmethod
    async def save_trade(self, trade: Trade) -> None:
        """Persist a trade.

        Args:
            trade: Trade to save

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_trades(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Get trade history with optional filters.

        Args:
            symbol: Optional symbol filter
            start_time: Optional start time filter
            end_time: Optional end time filter
            limit: Maximum number of trades to return

        Returns:
            List of trades

        Raises:
            StorageError: If retrieval fails
        """
        pass

    # Balance operations
    @abstractmethod
    async def save_balance_snapshot(self, balances: list[Balance]) -> None:
        """Save a snapshot of all balances.

        Args:
            balances: List of balances to snapshot

        Raises:
            StorageError: If save fails
        """
        pass

    @abstractmethod
    async def get_latest_balance_snapshot(self) -> list[Balance]:
        """Get the most recent balance snapshot.

        Returns:
            List of balances from latest snapshot

        Raises:
            StorageError: If retrieval fails
        """
        pass

    # Grid state operations (Stage 2.2)
    @abstractmethod
    async def save_grid_state(self, state: GridState) -> None:
        """Persist or replace the grid anchor for a symbol.

        Per ADR-006 decision 4, only ``GridState`` is persisted —
        ``GridSlot`` is a derived view computed each tick from
        ``compute_grid_levels`` plus a query of open orders.

        Idempotent: saving the same ``state`` twice leaves storage in
        the same shape (one row per symbol, last writer wins).

        Args:
            state: Grid anchor to persist.

        Raises:
            StorageError: If save fails.
        """
        pass

    @abstractmethod
    async def get_grid_state(self, symbol: Symbol) -> GridState | None:
        """Retrieve the grid anchor for a symbol.

        Args:
            symbol: Trading pair.

        Returns:
            ``GridState`` if the engine has previously initialized one
            for this symbol; ``None`` otherwise.

        Raises:
            StorageError: If retrieval fails.
        """
        pass

    # Price snapshot operations (Stage 3.0 — Observer mode)
    @abstractmethod
    async def save_price_snapshot(
        self,
        symbol: Symbol,
        price: Price,
        observed_at: Timestamp,
    ) -> None:
        """Append a single price observation to the snapshot history.

        ``cli/observe`` calls this on every poll. The history is the raw
        tape that ``DataCollector v2`` (Stage 3.1) will compute metrics
        over (volatility, returns, drawdown, etc.).

        Args:
            symbol: Trading pair the price is for.
            price: The observed price.
            observed_at: When the observation was made.

        Raises:
            StorageError: If save fails.
        """
        pass

    # News item operations (Stage 3.2.5 — News Ingestion)
    @abstractmethod
    async def save_news_item(self, item: NewsItem) -> None:
        """Persist a news item; idempotent on ``(source, external_id)``.

        Re-fetching the same item across polls is a no-op at the
        storage layer — the UNIQUE constraint on
        ``(source, external_id)`` causes a duplicate insert to be
        silently ignored. Items with ``external_id=None`` are always
        inserted (no dedup possible).

        Args:
            item: NewsItem to persist.

        Raises:
            StorageError: If save fails for reasons other than dedup
                (e.g. database unreachable).
        """

    # Advisor suggestion operations (Stage 3.3 — Passive Advisory Workflow)
    @abstractmethod
    async def save_advisor_suggestion(self, suggestion: AdvisorSuggestion) -> None:
        """Persist an advisor suggestion (recommendation + audit context).

        Per ADR-002 + ADR-007: nothing in this method or its readers
        auto-applies the recommendation. Persistence is for operator
        review (Stage 3.3) and future MoE aggregator history (3.4a).

        Args:
            suggestion: Complete persisted artifact.

        Raises:
            StorageError: If save fails.
        """

    @abstractmethod
    async def get_advisor_suggestions(
        self,
        since: datetime | None = None,
        model_name: str | None = None,
        role: str | None = None,
        limit: int | None = None,
    ) -> list[AdvisorSuggestion]:
        """Query persisted advisor suggestions with optional filters.

        Args:
            since: Lower bound on ``created_at`` (inclusive, tz-aware).
            model_name: Filter to one producing model (e.g.
                ``"phi4:14b"``).
            role: Filter to one role (``"single"``, ``"quant"``, etc.).
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching suggestions ordered by ``created_at`` DESC
            (newest first). Empty list if none match.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def save_applied_suggestion(self, applied: AppliedSuggestion) -> None:
        """Persist an audit row for a Stage 3.4b auto-applied suggestion.

        ``cli/apply --commit`` calls this once after the gate clears
        AND the settings.yml rewrite succeeds. The row carries
        per-key before/after deltas, the rejected keys (with reasons),
        and the originating model/rationale — so the operator can
        always answer "what did the bot change, why, and based on
        whose recommendation?"

        Args:
            applied: AppliedSuggestion audit row.

        Raises:
            StorageError: If save fails.
        """

    @abstractmethod
    async def get_applied_suggestions(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
        model_name: str | None = None,
        limit: int | None = None,
    ) -> list[AppliedSuggestion]:
        """Query auto-applied suggestion history.

        Args:
            since: Lower bound on ``applied_at`` (inclusive, tz-aware).
            symbol: Coin filter (e.g. ``"BTC"``).
            model_name: Filter to one producing model.
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching audit rows ordered by ``applied_at`` DESC.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def save_transfer_proposal(self, proposal: TransferProposal) -> None:
        """Persist a Stage 4.3 transfer proposal for operator review.

        Persisted regardless of ``HarvesterConfig.enabled`` — that flag
        gates execution (Stage 4.4+), not the forensic record. The
        proposal's ``proposal_id`` is enforced UNIQUE at the DB level
        to catch accidental double-inserts.

        Args:
            proposal: TransferProposal to save.

        Raises:
            StorageError: If save fails (including UNIQUE violation
                on ``proposal_id``).
        """

    @abstractmethod
    async def get_transfer_proposals(
        self,
        since: datetime | None = None,
        direction: str | None = None,
        asset: str | None = None,
        limit: int | None = None,
    ) -> list[TransferProposal]:
        """Query persisted transfer proposals with optional filters.

        Args:
            since: Lower bound on ``created_at`` (inclusive, tz-aware).
            direction: Filter to one direction
                (``"exchange_to_bank"`` or ``"bank_to_exchange"``).
            asset: Filter to one asset (``"USD"``, etc.).
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching proposals ordered by ``created_at`` DESC.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def save_transfer_result(self, result: TransferResult) -> None:
        """Persist a Stage 4.4b transfer result.

        ``transaction_id`` is UNIQUE — a retry that already wrote the
        row raises StorageError (the caller should fetch by id and
        skip rather than retry blindly).

        Args:
            result: TransferResult to save.

        Raises:
            StorageError: If save fails (including UNIQUE violation
                on ``transaction_id``).
        """

    @abstractmethod
    async def get_transfer_results(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        since: datetime | None = None,
        status: str | None = None,
        asset: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
    ) -> list[TransferResult]:
        """Query persisted transfer results with optional filters.

        Used by the Stage 4.4b day-cap helper to compute the rolling
        24h sum of exchange→bank withdrawals (``status != "failed"``,
        within window).

        Args:
            since: Lower bound on ``timestamp`` (inclusive, tz-aware).
            status: Filter to one status
                (``"pending"``, ``"completed"``, or ``"failed"``).
            asset: Filter to one asset.
            direction: Filter to one direction
                (``"exchange_to_bank"`` or ``"bank_to_exchange"``).
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching results ordered by ``timestamp`` DESC.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def get_news_items(
        self,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[NewsItem]:
        """Query news items with optional filters.

        Args:
            source: Optional source filter (e.g. ``"rss:coindesk"``).
            since: Optional lower bound on ``published_at``
                (inclusive). Tz-aware required.
            until: Optional upper bound on ``published_at``
                (inclusive). Tz-aware required.
            limit: Maximum number of rows to return. ``None`` means
                unbounded.

        Returns:
            Matching items ordered by ``published_at`` DESC (newest
            first — matches how advisor consumers want the data).
            Empty list if none match.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def get_price_snapshots(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
    ) -> list[PriceSnapshot]:
        """Read price-snapshot history with optional filters (Stage 3.1).

        ``DataCollector v2`` calls this to compute rolling metrics off
        the tape that ``cli/observe`` has been writing.

        Args:
            symbol: Optional symbol filter. When ``None``, returns
                snapshots for every symbol mixed together — rarely
                useful, but matches the ``get_trades`` pattern.
            start_time: Optional lower bound on ``observed_at``
                (inclusive). Must be tz-aware.
            end_time: Optional upper bound on ``observed_at``
                (inclusive). Must be tz-aware.
            limit: Maximum number of rows to return. ``None`` means
                unbounded — appropriate for windowed reads where the
                window itself caps the volume.

        Returns:
            Matching snapshots ordered by ``observed_at`` ascending
            (oldest first), so callers can pipe directly into a
            chronological series. Empty list if none match.

        Raises:
            StorageError: If retrieval fails.
        """
        pass

    # Pending command operations (Stage 5.4 — Operator interaction)
    @abstractmethod
    async def save_pending_command(self, pending: PendingCommand) -> None:
        """Persist a ``PendingCommand``; upserts on ``id``.

        ``cli/operator`` (Stage 5.6) writes a row on intent emission
        with ``status='awaiting_confirmation'``. Subsequent status
        transitions (``approved``, ``rejected``, ``expired``,
        ``dispatched``, ``failed``) re-save the same row via
        ``ON CONFLICT(id) DO UPDATE``. Per ADR-013 decision 4, this is
        the only mechanism by which intent flows from ``cli/operator``
        to ``cli/live``.

        Args:
            pending: Row to persist.

        Raises:
            StorageError: If save fails.
        """

    @abstractmethod
    async def get_pending_command(self, pending_id: UUID) -> PendingCommand | None:
        """Look up one ``PendingCommand`` by id.

        Args:
            pending_id: UUID primary key.

        Returns:
            The row if present; ``None`` if no such id exists.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def get_pending_commands(
        self,
        status: PendingCommandStatus | None = None,
        limit: int | None = None,
    ) -> list[PendingCommand]:
        """Query ``PendingCommand`` rows.

        ``cli/live`` polls with ``status='approved'`` every tick to
        pick up confirmed commands; operator inspection tools may pass
        ``status=None`` for the full audit log.

        Args:
            status: Optional status filter (one of the six lifecycle
                states from ``PendingCommandStatus``).
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching rows ordered by ``created_at`` ASC (oldest first)
            so a poll picks the longest-waiting approval first.

        Raises:
            StorageError: If retrieval fails.
        """

    # Notification operations (Stage 5.5 — outbound notifications)
    @abstractmethod
    async def save_notification(self, notification: Notification) -> int:
        """Persist a ``Notification`` row; returns the assigned row id.

        ``cli/live`` / ``cli/harvest`` (via ``SqliteNotifierAdapter``)
        call this on every outbound event. ``forwarded`` defaults to
        ``False`` at insert time and flips to ``True`` after
        ``cli/operator`` (Stage 5.6) successfully posts the row to
        Discord.

        Args:
            notification: Event payload.

        Returns:
            The row's auto-assigned integer id.

        Raises:
            StorageError: If save fails.
        """

    @abstractmethod
    async def get_notifications(
        self,
        forwarded: bool | None = None,
        limit: int | None = None,
    ) -> list[PersistedNotification]:
        """Query persisted notifications with optional forwarded filter.

        ``cli/operator`` polls with ``forwarded=False`` to drain rows
        for Discord posting; operator inspection may pass
        ``forwarded=None`` for the full audit log.

        Args:
            forwarded: Optional filter. ``False`` returns rows still
                awaiting Discord forward; ``True`` returns already-forwarded
                rows; ``None`` returns both.
            limit: Maximum rows to return. ``None`` means unbounded.

        Returns:
            Matching rows ordered by ``created_at`` ASC so the oldest
            unforwarded event posts first.

        Raises:
            StorageError: If retrieval fails.
        """

    @abstractmethod
    async def mark_notification_forwarded(
        self, notification_id: int, forwarded_at: Timestamp
    ) -> None:
        """Mark a notification row as forwarded to Discord.

        Idempotent — re-marking an already-forwarded row updates
        ``forwarded_at`` but is otherwise a no-op. ``cli/operator``
        calls this after a successful Discord post.

        Args:
            notification_id: Row id returned by ``save_notification``.
            forwarded_at: Timestamp of the successful Discord post.

        Raises:
            StorageError: If the update fails (including the row not
                existing).
        """

    # Conversation turn operations (Stage 5.6 — operator daemon)
    @abstractmethod
    async def save_conversation_turn(self, turn: ConversationTurn) -> None:
        """Persist one conversation turn; upserts on ``id``.

        ``cli/operator`` writes one row per inbound operator message
        (with ``intent_json`` populated after AssistantPort parsing)
        and one per outbound assistant reply. Stage 5.6's prompt
        assembler reads the last N turns for a given
        ``(channel_id, user_id)`` to build the LLM's context window.

        Args:
            turn: Row to persist.

        Raises:
            StorageError: If save fails.
        """

    @abstractmethod
    async def get_conversation_turns(
        self,
        channel_id: str,
        user_id: str,
        limit: int | None = None,
    ) -> list[ConversationTurn]:
        """Read recent conversation turns for one ``(channel_id, user_id)`` pair.

        Returned in chronological order (oldest first) so callers can
        feed the list directly to AssistantPort's prompt assembler.

        Args:
            channel_id: Discord channel scope.
            user_id: Discord user scope.
            limit: Maximum rows to return. ``None`` means unbounded —
                callers typically pass ``OperatorConfig.context_window_turns``
                to cap the prompt size.

        Returns:
            Matching turns ordered by ``timestamp`` ASC. Empty list if
            no turns exist for the scope yet.

        Raises:
            StorageError: If retrieval fails.
        """

"""SQLite implementation of StoragePort.

Persists orders, trades, and balance snapshots in a local SQLite database.
Decimal values are stored as TEXT to preserve precision (SQLite's REAL type
is double-precision float, which is lossy for monetary amounts).

Per ADR-005, orders use a dual-ID strategy: internal UUID for the primary key
and a nullable Kraken txid for cross-system identification.

Schema DDL lives in ``sqlite_storage_schema.py`` and row-to-domain mapping
helpers live in ``sqlite_storage_rowmap.py``; both were split out in Slice
5.1.C to keep this module under the pylint ``too-many-lines`` budget while
preserving the public ``SQLiteStorageAdapter`` interface unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import aiosqlite

from wobblebot.adapters.sqlite_storage_rowmap import (
    row_to_advisor_suggestion,
    row_to_applied_suggestion,
    row_to_news_item,
    row_to_order,
    row_to_pending_command,
    row_to_price_snapshot,
    row_to_trade,
    row_to_transfer_proposal,
    row_to_transfer_result,
    serialize_expert_opinions,
)
from wobblebot.adapters.sqlite_storage_schema import SCHEMA
from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Balance, NewsItem, Order, PriceSnapshot, Trade
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorSuggestion, AppliedSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.operator import PendingCommand, PendingCommandStatus
from wobblebot.ports.storage import StoragePort


class SQLiteStorageAdapter(StoragePort):  # pylint: disable=too-many-public-methods
    """SQLite-backed StoragePort implementation.

    The adapter holds a single long-lived ``aiosqlite.Connection``.
    Write statements (save_order, save_trade, save_balance_snapshot)
    are wrapped in a try/commit/rollback discipline so a mid-write
    failure cannot leave a dangling transaction on the connection.

    Per the StoragePort caller contract: callers MUST serialize
    per-entity writes themselves. This adapter offers no optimistic
    concurrency control. Concurrent ``get_order(X) -> mutate ->
    save_order(X)`` from two coroutines will produce a silent lost
    update — last writer wins.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and ensure schema exists.

        Creates any missing parent directories on the db path so a fresh
        operator who hasn't run ``mkdir data/`` yet doesn't hit a raw
        sqlite traceback. Special paths (``:memory:`` and the empty
        string Python uses for an anonymous on-disk DB) are passed
        through unchanged.
        """
        if self._conn is not None:
            return
        if self._db_path not in (":memory:", ""):
            parent = Path(self._db_path).expanduser().parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = await aiosqlite.connect(self._db_path)
            # Setting row_factory on the connection makes cursors inherit
            # it at execute() time; setting it on a cursor afterward is
            # unreliable and version-dependent in aiosqlite.
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.executescript(SCHEMA)
            await _migrate_advisor_suggestions_expert_opinions(self._conn)
            await self._conn.commit()
        except Exception as exc:
            raise StorageError(f"Failed to open database at {self._db_path}: {exc}") from exc

    async def close(self) -> None:
        """Close the underlying connection."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("Adapter is not connected; call connect() first")
        return self._conn

    async def save_order(self, order: Order) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO orders (
                    id, exchange_id,
                    symbol_base, symbol_quote, side,
                    price_amount, price_currency,
                    amount_value, amount_asset,
                    status, filled_amount,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    exchange_id = excluded.exchange_id,
                    status = excluded.status,
                    filled_amount = excluded.filled_amount,
                    updated_at = excluded.updated_at
                """,
                (
                    str(order.id),
                    order.exchange_id,
                    order.symbol.base,
                    order.symbol.quote,
                    order.side.value,
                    str(order.price.amount),
                    order.price.currency,
                    str(order.amount.value),
                    order.amount.asset,
                    order.status,
                    str(order.filled_amount),
                    order.created_at.dt.isoformat(),
                    order.updated_at.dt.isoformat() if order.updated_at else None,
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save order {order.id}: {exc}") from exc

    async def get_order(self, order_id: UUID) -> Order | None:
        conn = self._require_conn()
        try:
            async with conn.execute(
                "SELECT * FROM orders WHERE id = ?", (str(order_id),)
            ) as cursor:
                row = await cursor.fetchone()
            return row_to_order(row) if row else None
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load order {order_id}: {exc}") from exc

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]:
        conn = self._require_conn()
        sql = "SELECT * FROM orders WHERE status IN ('pending', 'open')"
        params: tuple[str, ...] = ()
        if symbol is not None:
            sql += " AND symbol_base = ? AND symbol_quote = ?"
            params = (symbol.base, symbol.quote)
        sql += " ORDER BY created_at"
        try:
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
            return [row_to_order(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load open orders: {exc}") from exc

    async def get_orders(
        self,
        symbol: Symbol | None = None,
        side: str | None = None,
        created_after: datetime | None = None,
    ) -> list[Order]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if symbol is not None:
            clauses.append("symbol_base = ? AND symbol_quote = ?")
            params.extend([symbol.base, symbol.quote])
        if side is not None:
            clauses.append("side = ?")
            params.append(side)
        if created_after is not None:
            clauses.append("created_at >= ?")
            params.append(created_after.astimezone(UTC).isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM orders{where} ORDER BY created_at"
        try:
            async with conn.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            return [row_to_order(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load orders: {exc}") from exc

    async def save_trade(self, trade: Trade) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT OR REPLACE INTO trades (
                    id, order_id,
                    symbol_base, symbol_quote, side,
                    price_amount, price_currency,
                    amount_value, amount_asset,
                    fee, cost, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.id,
                    trade.order_id,
                    trade.symbol.base,
                    trade.symbol.quote,
                    trade.side.value,
                    str(trade.price.amount),
                    trade.price.currency,
                    str(trade.amount.value),
                    trade.amount.asset,
                    str(trade.fee),
                    str(trade.cost),
                    trade.executed_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save trade {trade.id}: {exc}") from exc

    async def get_trades(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str | int] = []
        if symbol is not None:
            clauses.append("symbol_base = ? AND symbol_quote = ?")
            params.extend([symbol.base, symbol.quote])
        if start_time is not None:
            clauses.append("executed_at >= ?")
            params.append(start_time.astimezone(UTC).isoformat())
        if end_time is not None:
            clauses.append("executed_at <= ?")
            params.append(end_time.astimezone(UTC).isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM trades{where} ORDER BY executed_at DESC LIMIT ?"
        params.append(limit)
        try:
            async with conn.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            return [row_to_trade(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load trades: {exc}") from exc

    async def save_balance_snapshot(self, balances: list[Balance]) -> None:
        conn = self._require_conn()
        if not balances:
            raise StorageError("Cannot save an empty balance snapshot")
        snapshot_at = datetime.now(UTC).isoformat()
        try:
            async with conn.execute(
                "INSERT INTO balance_snapshots (snapshot_at) VALUES (?)",
                (snapshot_at,),
            ) as cursor:
                snapshot_id = cursor.lastrowid
            await conn.executemany(
                """
                INSERT INTO balance_entries
                    (snapshot_id, asset, total, available, locked, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        b.asset,
                        str(b.total),
                        str(b.available),
                        str(b.locked),
                        b.updated_at.dt.isoformat(),
                    )
                    for b in balances
                ],
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save balance snapshot: {exc}") from exc

    async def get_latest_balance_snapshot(self) -> list[Balance]:
        conn = self._require_conn()
        try:
            async with conn.execute("""
                SELECT asset, total, available, locked, updated_at
                FROM balance_entries
                WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM balance_snapshots)
                ORDER BY asset
                """) as cursor:
                rows = await cursor.fetchall()
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load latest balance snapshot: {exc}") from exc
        return [
            Balance(
                asset=row["asset"],
                total=Decimal(row["total"]),
                available=Decimal(row["available"]),
                locked=Decimal(row["locked"]),
                updated_at=Timestamp(dt=datetime.fromisoformat(row["updated_at"])),
            )
            for row in rows
        ]

    async def save_grid_state(self, state: GridState) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO grid_state (
                    symbol_base, symbol_quote,
                    reference_price, spacing_percentage,
                    levels_above, levels_below, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol_base, symbol_quote) DO UPDATE SET
                    reference_price = excluded.reference_price,
                    spacing_percentage = excluded.spacing_percentage,
                    levels_above = excluded.levels_above,
                    levels_below = excluded.levels_below,
                    created_at = excluded.created_at
                """,
                (
                    state.symbol.base,
                    state.symbol.quote,
                    str(state.reference_price),
                    str(state.spacing_percentage),
                    state.levels_above,
                    state.levels_below,
                    state.created_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save grid state for {state.symbol}: {exc}") from exc

    async def save_price_snapshot(
        self,
        symbol: Symbol,
        price: Price,
        observed_at: Timestamp,
    ) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO price_snapshots (
                    symbol_base, symbol_quote,
                    price_amount, price_currency, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    symbol.base,
                    symbol.quote,
                    str(price.amount),
                    price.currency,
                    observed_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save price snapshot for {symbol}: {exc}") from exc

    async def save_news_item(self, item: NewsItem) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT OR IGNORE INTO news_items (
                    source, external_id, published_at, headline,
                    body, sentiment_score, mentioned_coins, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source,
                    item.external_id,
                    item.published_at.dt.isoformat(),
                    item.headline,
                    item.body,
                    item.sentiment_score,
                    json.dumps(item.mentioned_coins),
                    item.fetched_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save news item from {item.source}: {exc}") from exc

    async def save_advisor_suggestion(self, suggestion: AdvisorSuggestion) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO advisor_suggestions (
                    recommendation_id, created_at, role,
                    recommendations, rationale, confidence,
                    input_summary, model_name, expert_opinions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion.recommendation.recommendation_id,
                    suggestion.created_at.dt.isoformat(),
                    suggestion.recommendation.role,
                    json.dumps(suggestion.recommendation.recommendations),
                    suggestion.recommendation.rationale,
                    suggestion.recommendation.confidence,
                    json.dumps(suggestion.input_summary),
                    suggestion.model_name,
                    serialize_expert_opinions(suggestion.recommendation.expert_opinions),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(
                f"Failed to save advisor suggestion from {suggestion.model_name}: {exc}"
            ) from exc

    async def get_advisor_suggestions(
        self,
        since: datetime | None = None,
        model_name: str | None = None,
        role: str | None = None,
        limit: int | None = None,
    ) -> list[AdvisorSuggestion]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if model_name is not None:
            clauses.append("model_name = ?")
            params.append(model_name)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM advisor_suggestions{where} ORDER BY created_at DESC"
        bound: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound = (*bound, limit)
        try:
            async with conn.execute(sql, bound) as cursor:
                rows = await cursor.fetchall()
            return [row_to_advisor_suggestion(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load advisor suggestions: {exc}") from exc

    async def save_applied_suggestion(self, applied: AppliedSuggestion) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO applied_suggestions (
                    recommendation_id, applied_at, symbol,
                    applied_keys, rejected_keys, model_name, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    applied.recommendation_id,
                    applied.applied_at.dt.isoformat(),
                    applied.symbol,
                    json.dumps(applied.applied_keys),
                    json.dumps(applied.rejected_keys),
                    applied.model_name,
                    applied.rationale,
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(
                f"Failed to save applied suggestion for {applied.recommendation_id}: {exc}"
            ) from exc

    async def get_applied_suggestions(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
        model_name: str | None = None,
        limit: int | None = None,
    ) -> list[AppliedSuggestion]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("applied_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if model_name is not None:
            clauses.append("model_name = ?")
            params.append(model_name)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM applied_suggestions{where} ORDER BY applied_at DESC"
        bound: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound = (*bound, limit)
        try:
            async with conn.execute(sql, bound) as cursor:
                rows = await cursor.fetchall()
            return [row_to_applied_suggestion(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load applied suggestions: {exc}") from exc

    async def save_transfer_proposal(self, proposal: TransferProposal) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO transfer_proposals (
                    proposal_id, direction, asset, amount, rationale,
                    current_exchange_balance, target_exchange_balance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.direction,
                    proposal.asset,
                    str(proposal.amount),
                    proposal.rationale,
                    str(proposal.current_exchange_balance),
                    str(proposal.target_exchange_balance),
                    proposal.created_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(
                f"Failed to save transfer proposal {proposal.proposal_id}: {exc}"
            ) from exc

    async def get_transfer_proposals(
        self,
        since: datetime | None = None,
        direction: str | None = None,
        asset: str | None = None,
        limit: int | None = None,
    ) -> list[TransferProposal]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        if asset is not None:
            clauses.append("asset = ?")
            params.append(asset)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM transfer_proposals{where} ORDER BY created_at DESC"
        bound: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound = (*bound, limit)
        try:
            async with conn.execute(sql, bound) as cursor:
                rows = await cursor.fetchall()
            return [row_to_transfer_proposal(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load transfer proposals: {exc}") from exc

    async def save_transfer_result(self, result: TransferResult) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO transfer_results (
                    proposal_id, transaction_id, status, executed_amount,
                    direction, asset, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.proposal_id,
                    result.transaction_id,
                    result.status,
                    str(result.executed_amount),
                    result.direction,
                    result.asset,
                    result.timestamp.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(
                f"Failed to save transfer result {result.transaction_id}: {exc}"
            ) from exc

    async def get_transfer_results(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        since: datetime | None = None,
        status: str | None = None,
        asset: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
    ) -> list[TransferResult]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if asset is not None:
            clauses.append("asset = ?")
            params.append(asset)
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM transfer_results{where} ORDER BY timestamp DESC"
        bound: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound = (*bound, limit)
        try:
            async with conn.execute(sql, bound) as cursor:
                rows = await cursor.fetchall()
            return [row_to_transfer_result(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load transfer results: {exc}") from exc

    async def get_news_items(
        self,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[NewsItem]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if since is not None:
            clauses.append("published_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until is not None:
            clauses.append("published_at <= ?")
            params.append(until.astimezone(UTC).isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM news_items{where} ORDER BY published_at DESC"
        bound_params: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound_params = (*bound_params, limit)
        try:
            async with conn.execute(sql, bound_params) as cursor:
                rows = await cursor.fetchall()
            return [row_to_news_item(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load news items: {exc}") from exc

    async def get_price_snapshots(
        self,
        symbol: Symbol | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
    ) -> list[PriceSnapshot]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if symbol is not None:
            clauses.append("symbol_base = ? AND symbol_quote = ?")
            params.extend([symbol.base, symbol.quote])
        if start_time is not None:
            clauses.append("observed_at >= ?")
            params.append(start_time.astimezone(UTC).isoformat())
        if end_time is not None:
            clauses.append("observed_at <= ?")
            params.append(end_time.astimezone(UTC).isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM price_snapshots{where} ORDER BY observed_at"
        bound_params: tuple[str | int, ...] = tuple(params)
        if limit is not None:
            sql += " LIMIT ?"
            bound_params = (*bound_params, limit)
        try:
            async with conn.execute(sql, bound_params) as cursor:
                rows = await cursor.fetchall()
            return [row_to_price_snapshot(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load price snapshots: {exc}") from exc

    async def get_grid_state(self, symbol: Symbol) -> GridState | None:
        conn = self._require_conn()
        try:
            async with conn.execute(
                """
                SELECT * FROM grid_state
                WHERE symbol_base = ? AND symbol_quote = ?
                """,
                (symbol.base, symbol.quote),
            ) as cursor:
                row = await cursor.fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load grid state for {symbol}: {exc}") from exc
        if row is None:
            return None
        return GridState(
            symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
            reference_price=Decimal(row["reference_price"]),
            spacing_percentage=Decimal(row["spacing_percentage"]),
            levels_above=row["levels_above"],
            levels_below=row["levels_below"],
            created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
        )

    # ----- pending commands (Stage 5.4 — operator interaction) -----

    async def save_pending_command(self, pending: PendingCommand) -> None:
        conn = self._require_conn()
        command_json = pending.command.model_dump_json()
        result_json = pending.result.model_dump_json() if pending.result else None
        try:
            await conn.execute(
                """
                INSERT INTO pending_commands (
                    id, command_kind, command_json, status,
                    channel_id, requesting_user_id,
                    confirming_user_id, confirmed_at,
                    dispatched_at, result_json,
                    ttl_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    command_kind = excluded.command_kind,
                    command_json = excluded.command_json,
                    status = excluded.status,
                    confirming_user_id = excluded.confirming_user_id,
                    confirmed_at = excluded.confirmed_at,
                    dispatched_at = excluded.dispatched_at,
                    result_json = excluded.result_json,
                    ttl_expires_at = excluded.ttl_expires_at
                """,
                (
                    str(pending.id),
                    pending.command.kind,
                    command_json,
                    pending.status,
                    pending.channel_id,
                    pending.requesting_user_id,
                    pending.confirming_user_id,
                    pending.confirmed_at.dt.isoformat() if pending.confirmed_at else None,
                    pending.dispatched_at.dt.isoformat() if pending.dispatched_at else None,
                    result_json,
                    pending.ttl_expires_at.dt.isoformat(),
                    pending.created_at.dt.isoformat(),
                ),
            )
            await conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            await conn.rollback()
            raise StorageError(f"Failed to save pending command {pending.id}: {exc}") from exc

    async def get_pending_command(self, pending_id: UUID) -> PendingCommand | None:
        conn = self._require_conn()
        try:
            async with conn.execute(
                "SELECT * FROM pending_commands WHERE id = ?", (str(pending_id),)
            ) as cursor:
                row = await cursor.fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load pending command {pending_id}: {exc}") from exc
        return row_to_pending_command(row) if row else None

    async def get_pending_commands(
        self,
        status: PendingCommandStatus | None = None,
        limit: int | None = None,
    ) -> list[PendingCommand]:
        conn = self._require_conn()
        sql = "SELECT * FROM pending_commands"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load pending commands: {exc}") from exc
        return [row_to_pending_command(row) for row in rows]


async def _migrate_advisor_suggestions_expert_opinions(
    conn: aiosqlite.Connection,
) -> None:
    """Add the ``expert_opinions`` column to pre-3.4a advisor_suggestions tables.

    The CREATE TABLE in ``SCHEMA`` (sqlite_storage_schema.py) already declares the column for new
    DBs (via ``IF NOT EXISTS``), but operators running Stage 3.3 have
    existing tables that lack it. SQLite doesn't support ``ALTER TABLE
    ADD COLUMN IF NOT EXISTS``, so we PRAGMA-check first.
    """
    async with conn.execute("PRAGMA table_info(advisor_suggestions)") as cursor:
        cols = {row[1] async for row in cursor}
    if "expert_opinions" not in cols:
        await conn.execute(
            "ALTER TABLE advisor_suggestions "
            "ADD COLUMN expert_opinions TEXT NOT NULL DEFAULT '[]'"
        )

"""SQLite implementation of StoragePort.

Persists orders, trades, and balance snapshots in a local SQLite database.
Decimal values are stored as TEXT to preserve precision (SQLite's REAL type
is double-precision float, which is lossy for monetary amounts).

Per ADR-005, orders use a dual-ID strategy: internal UUID for the primary key
and a nullable Kraken txid for cross-system identification.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import aiosqlite

from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Balance, NewsItem, Order, PriceSnapshot, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    exchange_id     TEXT,
    symbol_base     TEXT NOT NULL,
    symbol_quote    TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price_amount    TEXT NOT NULL,
    price_currency  TEXT NOT NULL,
    amount_value    TEXT NOT NULL,
    amount_asset    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('pending', 'open', 'closed', 'canceled', 'expired')),
    filled_amount   TEXT NOT NULL DEFAULT '0',
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_exchange_id ON orders(exchange_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol
    ON orders(symbol_base, symbol_quote);

CREATE TABLE IF NOT EXISTS trades (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    symbol_base     TEXT NOT NULL,
    symbol_quote    TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price_amount    TEXT NOT NULL,
    price_currency  TEXT NOT NULL,
    amount_value    TEXT NOT NULL,
    amount_asset    TEXT NOT NULL,
    fee             TEXT NOT NULL,
    cost            TEXT NOT NULL,
    executed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades(executed_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol
    ON trades(symbol_base, symbol_quote);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance_entries (
    snapshot_id     INTEGER NOT NULL
                    REFERENCES balance_snapshots(snapshot_id) ON DELETE CASCADE,
    asset           TEXT NOT NULL,
    total           TEXT NOT NULL,
    available       TEXT NOT NULL,
    locked          TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, asset)
);

CREATE TABLE IF NOT EXISTS grid_state (
    symbol_base         TEXT NOT NULL,
    symbol_quote        TEXT NOT NULL,
    reference_price     TEXT NOT NULL,
    spacing_percentage  TEXT NOT NULL,
    levels_above        INTEGER NOT NULL,
    levels_below        INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (symbol_base, symbol_quote)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_base     TEXT NOT NULL,
    symbol_quote    TEXT NOT NULL,
    price_amount    TEXT NOT NULL,
    price_currency  TEXT NOT NULL,
    observed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_symbol_time
    ON price_snapshots(symbol_base, symbol_quote, observed_at);

CREATE TABLE IF NOT EXISTS news_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    external_id     TEXT,
    published_at    TEXT NOT NULL,
    headline        TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    sentiment_score REAL,
    mentioned_coins TEXT NOT NULL DEFAULT '[]',
    fetched_at      TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_news_items_published
    ON news_items(published_at);
CREATE INDEX IF NOT EXISTS idx_news_items_source_time
    ON news_items(source, published_at);

CREATE TABLE IF NOT EXISTS advisor_suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    role                TEXT NOT NULL,
    recommendations     TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    confidence          TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    input_summary       TEXT NOT NULL,
    model_name          TEXT NOT NULL,
    -- Stage 3.4a: MoE per-expert audit trail. JSON array of opinion dicts
    -- (role, confidence, recommendations, rationale). Empty array for
    -- single-LLM suggestions. NOT NULL DEFAULT keeps the migration on
    -- pre-3.4a DBs trivial — the ALTER below picks up existing rows.
    expert_opinions     TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_advisor_suggestions_created
    ON advisor_suggestions(created_at);
CREATE INDEX IF NOT EXISTS idx_advisor_suggestions_model
    ON advisor_suggestions(model_name, created_at);
"""


class SQLiteStorageAdapter(StoragePort):
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
            await self._conn.executescript(_SCHEMA)
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
            return _row_to_order(row) if row else None
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
            return [_row_to_order(row) for row in rows]
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
            return [_row_to_order(row) for row in rows]
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
            return [_row_to_trade(row) for row in rows]
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
                    _serialize_expert_opinions(suggestion.recommendation.expert_opinions),
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
            return [_row_to_advisor_suggestion(row) for row in rows]
        except (aiosqlite.Error, OSError) as exc:
            raise StorageError(f"Failed to load advisor suggestions: {exc}") from exc

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
            return [_row_to_news_item(row) for row in rows]
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
            return [_row_to_price_snapshot(row) for row in rows]
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


def _row_to_order(row: aiosqlite.Row) -> Order:
    return Order(
        id=UUID(row["id"]),
        exchange_id=row["exchange_id"],
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        side=OrderSide(row["side"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        amount=Amount(value=Decimal(row["amount_value"]), asset=row["amount_asset"]),
        status=row["status"],
        filled_amount=Decimal(row["filled_amount"]),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
        updated_at=(
            Timestamp(dt=datetime.fromisoformat(row["updated_at"])) if row["updated_at"] else None
        ),
    )


def _row_to_trade(row: aiosqlite.Row) -> Trade:
    return Trade(
        id=row["id"],
        order_id=row["order_id"],
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        side=OrderSide(row["side"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        amount=Amount(value=Decimal(row["amount_value"]), asset=row["amount_asset"]),
        fee=Decimal(row["fee"]),
        cost=Decimal(row["cost"]),
        executed_at=Timestamp(dt=datetime.fromisoformat(row["executed_at"])),
    )


def _row_to_price_snapshot(row: aiosqlite.Row) -> PriceSnapshot:
    return PriceSnapshot(
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        observed_at=Timestamp(dt=datetime.fromisoformat(row["observed_at"])),
    )


def _row_to_news_item(row: aiosqlite.Row) -> NewsItem:
    return NewsItem(
        source=row["source"],
        external_id=row["external_id"],
        published_at=Timestamp(dt=datetime.fromisoformat(row["published_at"])),
        headline=row["headline"],
        body=row["body"] or "",
        sentiment_score=row["sentiment_score"],
        mentioned_coins=json.loads(row["mentioned_coins"]),
        fetched_at=Timestamp(dt=datetime.fromisoformat(row["fetched_at"])),
    )


def _row_to_advisor_suggestion(row: aiosqlite.Row) -> AdvisorSuggestion:
    expert_opinions_raw = row["expert_opinions"] if "expert_opinions" in row.keys() else "[]"
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(
            recommendation_id=row["recommendation_id"],
            timestamp=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
            role=row["role"],
            recommendations=json.loads(row["recommendations"]),
            rationale=row["rationale"],
            confidence=row["confidence"],
            expert_opinions=_deserialize_expert_opinions(
                expert_opinions_raw,
                fallback_timestamp=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
            ),
        ),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
        input_summary=json.loads(row["input_summary"]),
        model_name=row["model_name"],
    )


def _serialize_expert_opinions(opinions: list[AdvisorRecommendation]) -> str:
    """Serialize per-expert opinions for the audit-trail column.

    Stored as a JSON array of dicts (one per expert). The forensic
    fields are: role, confidence, recommendations, rationale. The
    ``recommendation_id`` and ``timestamp`` per-expert get dropped on
    purpose — they're synthetic UUIDs / per-call wall-clocks that
    carry no semantic information once the aggregated row exists. If
    we ever need them, the column is JSON so a future migration can
    add them back without a schema change.
    """
    return json.dumps(
        [
            {
                "role": op.role,
                "confidence": op.confidence,
                "recommendations": op.recommendations,
                "rationale": op.rationale,
            }
            for op in opinions
        ]
    )


def _deserialize_expert_opinions(
    raw: str,
    *,
    fallback_timestamp: Timestamp,
) -> list[AdvisorRecommendation]:
    """Reverse :func:`_serialize_expert_opinions`.

    We reconstruct ``AdvisorRecommendation`` instances with synthetic
    ``recommendation_id`` (``"opinion-<idx>"``) and the parent row's
    timestamp — neither field was persisted per-opinion. Read-side
    consumers (``tools/show_suggestions.py``) care about role /
    confidence / recommendations / rationale, not the synthetic IDs.
    """
    if not raw:
        return []
    payload = json.loads(raw)
    return [
        AdvisorRecommendation(
            recommendation_id=f"opinion-{idx}",
            timestamp=fallback_timestamp,
            role=entry["role"],
            recommendations=entry.get("recommendations") or {},
            rationale=entry["rationale"],
            confidence=entry["confidence"],
        )
        for idx, entry in enumerate(payload)
    ]


async def _migrate_advisor_suggestions_expert_opinions(
    conn: aiosqlite.Connection,
) -> None:
    """Add the ``expert_opinions`` column to pre-3.4a advisor_suggestions tables.

    The CREATE TABLE in ``_SCHEMA`` already declares the column for new
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

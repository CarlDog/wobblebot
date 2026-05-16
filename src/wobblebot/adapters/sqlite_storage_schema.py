"""SQLite schema DDL for ``SQLiteStorageAdapter``.

Extracted from ``sqlite_storage.py`` to keep the adapter module under the
project's per-file line budget (1000 lines, pylint ``too-many-lines``).
The schema is one logical concern — every ``CREATE TABLE`` and
``CREATE INDEX`` the adapter needs at first connect — so a sibling
module makes more sense than splitting it across multiple files by
table family.

The constant is consumed by ``SQLiteStorageAdapter.connect()`` via
``executescript(SCHEMA)`` on a fresh DB; existing DBs see ``IF NOT
EXISTS`` no-op the statements. Any in-place column additions for older
DBs (Stage 3.4a's ``expert_opinions``) ride in their own ``ALTER
TABLE`` migration paths inside the adapter — schema constants are
declarative, migrations are imperative.
"""

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS applied_suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id   TEXT NOT NULL,
    applied_at          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    applied_keys        TEXT NOT NULL DEFAULT '[]',
    rejected_keys       TEXT NOT NULL DEFAULT '[]',
    model_name          TEXT NOT NULL,
    rationale           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applied_suggestions_applied
    ON applied_suggestions(applied_at);
CREATE INDEX IF NOT EXISTS idx_applied_suggestions_symbol
    ON applied_suggestions(symbol, applied_at);

CREATE TABLE IF NOT EXISTS transfer_proposals (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id                 TEXT NOT NULL UNIQUE,
    direction                   TEXT NOT NULL
                                    CHECK (direction IN ('exchange_to_bank', 'bank_to_exchange')),
    asset                       TEXT NOT NULL,
    amount                      TEXT NOT NULL,
    rationale                   TEXT NOT NULL,
    current_exchange_balance    TEXT NOT NULL,
    target_exchange_balance     TEXT NOT NULL,
    created_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfer_proposals_created
    ON transfer_proposals(created_at);
CREATE INDEX IF NOT EXISTS idx_transfer_proposals_direction
    ON transfer_proposals(direction, created_at);

CREATE TABLE IF NOT EXISTS transfer_results (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id                 TEXT NOT NULL,
    transaction_id              TEXT NOT NULL UNIQUE,
    status                      TEXT NOT NULL
                                    CHECK (status IN ('pending', 'completed', 'failed')),
    executed_amount             TEXT NOT NULL,
    direction                   TEXT NOT NULL
                                    CHECK (direction IN ('exchange_to_bank', 'bank_to_exchange')),
    asset                       TEXT NOT NULL,
    timestamp                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfer_results_timestamp
    ON transfer_results(timestamp);
CREATE INDEX IF NOT EXISTS idx_transfer_results_day_cap
    ON transfer_results(asset, direction, timestamp);
"""

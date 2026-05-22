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

-- Stage 5.4 — pending commands (operator interaction layer, ADR-013).
-- cli/operator writes; cli/live polls WHERE status='approved'.
-- The full OperatorCommand and (optional) CommandResult ride as JSON
-- so future command/result schema evolution doesn't force a migration.
-- command_kind is denormalized for selective filtering / metrics.
CREATE TABLE IF NOT EXISTS pending_commands (
    id                  TEXT PRIMARY KEY,
    command_kind        TEXT NOT NULL,
    command_json        TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'awaiting_confirmation', 'approved', 'rejected',
                            'expired', 'dispatched', 'failed'
                        )),
    channel_id          TEXT NOT NULL,
    requesting_user_id  TEXT NOT NULL,
    confirming_user_id  TEXT,
    confirmed_at        TEXT,
    dispatched_at       TEXT,
    result_json         TEXT,
    ttl_expires_at      TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_commands_status
    ON pending_commands(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_commands_created
    ON pending_commands(created_at);
CREATE INDEX IF NOT EXISTS idx_pending_commands_ttl
    ON pending_commands(ttl_expires_at);

-- Stage 5.5 — outbound notifications (operator interaction layer, ADR-013).
-- cli/live and cli/harvest write rows via SqliteNotifierAdapter;
-- cli/operator polls forwarded=0 rows and posts each to Discord.
-- context_json holds the Notification's structured context dict;
-- forwarded / forwarded_at track Discord forwarding status independent
-- of the originating write.
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error', 'critical')),
    title           TEXT NOT NULL,
    message         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    context_json    TEXT NOT NULL DEFAULT '{}',
    forwarded       INTEGER NOT NULL DEFAULT 0 CHECK (forwarded IN (0, 1)),
    forwarded_at    TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_forwarded
    ON notifications(forwarded, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_timestamp
    ON notifications(timestamp);

-- Stage 5.6 — conversation history per Discord (channel, user) pair.
-- cli/operator persists every operator + assistant turn for multi-turn
-- prompt assembly + forensic audit. intent_json is populated for
-- operator turns once parsed by AssistantPort, NULL for assistant
-- turns and for operator turns that haven't been parsed yet.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('operator', 'assistant')),
    content         TEXT NOT NULL,
    intent_json     TEXT,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_scope
    ON conversation_turns(channel_id, user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_timestamp
    ON conversation_turns(timestamp);

-- Stage 6.1 — cloud-LLM forensic cost ledger (Phase 6, ADR-014).
-- One row per cloud-LLM API call; Ollama (free / local) calls bypass.
-- The 24h-window cost gate in services/llm_cost_gate reads
-- (timestamp, cost_usd) for its sliding-window total; per-provider /
-- per-role rollups in tools/show_llm_costs use the secondary indexes.
-- success / error_kind capture failed-but-billed calls (some providers
-- charge for content-policy refusals).
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN (
                            'operator', 'quant', 'risk', 'news',
                            'arbitrator', 'single', 'unknown'
                        )),
    provider            TEXT NOT NULL CHECK (provider IN (
                            'anthropic', 'openai', 'google'
                        )),
    model               TEXT NOT NULL,
    tokens_in           INTEGER NOT NULL,
    tokens_out          INTEGER NOT NULL,
    tokens_reasoning    INTEGER,
    cost_usd            TEXT NOT NULL,
    request_id          TEXT,
    success             INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_kind          TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_timestamp
    ON llm_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_calls_provider_model
    ON llm_calls(provider, model, timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_calls_role
    ON llm_calls(role, timestamp);

-- Stage 7.1 — operator accounts for the Phase 7 web UI (ADR-017).
-- v1 has one row in production; the UNIQUE(username) index supports
-- the login-lookup path. password_hash is the $2b$-prefixed bcrypt
-- output (~60 chars); the plaintext password is NEVER stored
-- anywhere. The CHECK guard catches "empty hash" misuse at the SQL
-- layer; Pydantic's min_length on User.password_hash is the
-- primary defense.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL CHECK (length(password_hash) > 0),
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- Stage 8.4 follow-up — per-user web UI preferences. Separated
-- from the users table so identity vs. UI-presentation concerns
-- don't share row width as preferences accumulate (timezone now;
-- per-card refresh cadences, default dashboard layout, etc. on the
-- v1.1 backlog). ON DELETE CASCADE keeps the table free of orphan
-- rows when an operator account is removed.
--
-- The `timezone` column stores an IANA tz database name (e.g.
-- "America/Chicago", "Europe/London", "UTC"). Python's stdlib
-- zoneinfo (PEP 615) reads these directly. Validation happens at
-- the route layer — the operator picks from a dropdown or types a
-- valid IANA string; bad strings are rejected before save.
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id     INTEGER PRIMARY KEY
                REFERENCES users(id) ON DELETE CASCADE,
    timezone    TEXT NOT NULL DEFAULT 'UTC' CHECK (length(timezone) > 0),
    updated_at  TEXT NOT NULL
);

-- Stage 8.4.E follow-up — daemon heartbeat ledger.
-- Each long-running daemon upserts its row at the top of its tick
-- loop; cli/web's /health page reads the table and classifies
-- freshness against a per-daemon threshold derived from the
-- corresponding configured cadence (live.tick_seconds,
-- schedules.harvest, operator.forwarder_poll_seconds,
-- min(schedules.maintenance_*)). One row per daemon; UPSERT keeps
-- the ledger tiny.
--
-- Lives in operator.db (the home for cross-daemon coordination
-- state, alongside pending_commands and notifications). Daemons
-- that don't already open operator.db (cli/maintenance) opt in via
-- a new operator_db config field.
CREATE TABLE IF NOT EXISTS daemon_heartbeats (
    name            TEXT PRIMARY KEY CHECK (length(name) > 0),
    last_beat_at    TEXT NOT NULL
);
"""

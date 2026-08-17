"""Additive schema migrations for the SQLite storage adapter.

Extracted from ``sqlite_storage.py`` 2026-08-15 (the adapter had grown
to ~2000 lines; the migration block was the one clean seam — standalone
functions with no dependence on the adapter class). Behavior is
IDENTICAL: ``SQLiteStorageAdapter.connect()`` calls each ``_migrate_*``
in the same order it always did, after ``SCHEMA`` applies.

The contract every function here honors:

- **Additive and idempotent.** Safe to run on every connect, against a
  fresh DB (where ``SCHEMA`` already declared the end state and the
  migration no-ops) and against any operator DB written by any earlier
  version.
- **Race-tolerant.** Multiple daemons open the same DB file at boot
  (the compose stack cold-start), so "lost the race, state is already
  correct" must never surface as a failure — see
  ``add_column_if_missing``.
- **Forensic data is never silently destroyed.** A migration may
  collapse true junk (``migrate_price_snapshots_unique``'s duplicate
  snapshots) but must refuse and log loudly where a duplicate could be
  evidence (``migrate_transfer_results_unique_proposal_id``).
"""

from __future__ import annotations

import logging

import aiosqlite

_LOGGER = logging.getLogger("wobblebot.adapters.sqlite_migrations")


async def add_column_if_missing(
    conn: aiosqlite.Connection, table: str, column: str, ddl_suffix: str
) -> None:
    """Add ``column`` to ``table`` if missing, tolerating a concurrent-process race.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so every
    additive migration in this module does PRAGMA-check-then-ALTER. When
    two processes open the same DB file for the first time after a new
    migration ships (e.g. every daemon in the compose stack cold-booting
    at once), both can pass the PRAGMA check before either's ALTER
    commits -- the loser then hits "duplicate column name" even though
    the column is now genuinely present and the migration's goal is
    satisfied. Re-checking PRAGMA table_info after a failed ALTER
    (rather than string-matching the error) distinguishes "lost this
    race, column's there, fine" from a real failure. Traces back to the
    2026-08-05 outage: that race hit a since-fixed bug in an unrelated
    warning-log call and turned into a full dashboard crash instead of
    the harmless race it actually was.
    """
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        cols = {row[1] async for row in cursor}
    if column in cols:
        return
    try:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")
    except aiosqlite.Error:
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            cols_after = {row[1] async for row in cursor}
        if column not in cols_after:
            raise


async def migrate_advisor_suggestions_expert_opinions(
    conn: aiosqlite.Connection,
) -> None:
    """Add the ``expert_opinions`` column to pre-3.4a advisor_suggestions tables.

    The CREATE TABLE in ``SCHEMA`` (sqlite_storage_schema.py) already declares the column for new
    DBs (via ``IF NOT EXISTS``), but operators running Stage 3.3 have
    existing tables that lack it. SQLite doesn't support ``ALTER TABLE
    ADD COLUMN IF NOT EXISTS``, so we PRAGMA-check first.
    """
    await add_column_if_missing(
        conn, "advisor_suggestions", "expert_opinions", "TEXT NOT NULL DEFAULT '[]'"
    )


async def migrate_advisor_suggestions_news_materially_drove(
    conn: aiosqlite.Connection,
) -> None:
    """Add the ``news_materially_drove`` column (ADR-007 amendment, v1.1).

    Same shape as ``migrate_advisor_suggestions_expert_opinions`` --
    ``SCHEMA`` already declares the column for new DBs; operators on a
    pre-amendment table need the ``ALTER``. Existing rows default to 0
    (not news-driven) -- correct for the forensic record since the
    flag didn't exist when those rows were written, and re-computing it
    retroactively isn't possible without the original per-expert
    opinions in a comparable shape.
    """
    await add_column_if_missing(
        conn,
        "advisor_suggestions",
        "news_materially_drove",
        "INTEGER NOT NULL DEFAULT 0 CHECK (news_materially_drove IN (0, 1))",
    )


async def migrate_news_items_publisher_url(conn: aiosqlite.Connection) -> None:
    """Add ``publisher`` + ``url`` columns to pre-2026-05-23 news_items tables.

    The CREATE TABLE in SCHEMA declares both for new DBs; pre-existing
    operator news_items rows lack them. Additive only (TEXT NULL); the
    3882+ existing rows stay valid with both columns set to NULL on
    read.
    """
    await add_column_if_missing(conn, "news_items", "publisher", "TEXT")
    await add_column_if_missing(conn, "news_items", "url", "TEXT")


async def migrate_llm_calls_cache_token_columns(conn: aiosqlite.Connection) -> None:
    """Add cache-token columns to pre-ADR-033 llm_calls tables.

    The CREATE TABLE in SCHEMA declares both for new DBs; existing
    operator ledgers lack them. Additive ``INTEGER NOT NULL DEFAULT 0``
    — SQLite backfills existing rows with 0, which is the correct
    forensic reading (cache counts weren't captured when those rows
    were written). Definitions must stay byte-identical to the CREATE
    TABLE so fresh and migrated DBs don't drift.
    """
    await add_column_if_missing(
        conn, "llm_calls", "tokens_cache_read", "INTEGER NOT NULL DEFAULT 0"
    )
    await add_column_if_missing(
        conn, "llm_calls", "tokens_cache_write", "INTEGER NOT NULL DEFAULT 0"
    )


async def migrate_llm_calls_trace_id(conn: aiosqlite.Connection) -> None:
    """Add ``trace_id`` to pre-P4.1 llm_calls tables (per-cycle tracing).

    SCHEMA declares the column for fresh DBs; existing ledgers lack it.
    Nullable TEXT — rows written before tracing existed honestly have
    no trace, and NULL says so. Rides the outcome-ledger migration
    event per the v1.1 register ("ship together").
    """
    await add_column_if_missing(conn, "llm_calls", "trace_id", "TEXT")


async def migrate_notifications_read_at(conn: aiosqlite.Connection) -> None:
    """Add ``read_at`` + the unread partial index (P3 slice 19).

    SCHEMA declares the column for fresh DBs, so the ALTER no-ops
    there; operator DBs written before this slice need it. Existing
    rows land on NULL = unread, which is the honest reading — nobody
    had a way to acknowledge them.

    The index lives here rather than in SCHEMA because SCHEMA runs
    (executescript) BEFORE any migration, so indexing ``read_at``
    there would raise "no such column" against a pre-slice DB. It is
    PARTIAL (``WHERE read_at IS NULL``) because the only query that
    needs it is the bell badge's unread count, polled every 30s per
    open tab forever: a partial index holds only the unread rows, so
    the count stays an index-only scan that shrinks as the operator
    acknowledges instead of growing with the table.
    """
    await add_column_if_missing(conn, "notifications", "read_at", "TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_unread "
        "ON notifications(read_at) WHERE read_at IS NULL"
    )


async def migrate_price_snapshots_unique(  # pylint: disable=too-many-locals
    conn: aiosqlite.Connection,
) -> None:
    """Make ``price_snapshots`` idempotent on ``(symbol, observed_at)``.

    v1.1 backfill follow-up (2026-05-25). The UNIQUE index is NOT
    declared in SCHEMA because a pre-existing observe.db with
    duplicate rows would fail at schema apply before this function
    could dedup. Sequence:

    1. Count existing duplicates (cheap aggregate scan).
    2. If duplicates exist, log a WARN with the count and DELETE
       all but ``MIN(snapshot_id)`` per (symbol, observed_at) group.
    3. Create the UNIQUE INDEX (IF NOT EXISTS makes it idempotent
       across re-runs).

    In practice the daemon writes ``datetime.now(UTC)`` with
    microsecond precision -- duplicates basically don't exist -- so
    step 2 is almost always a no-op. The WARN exists so the operator
    notices unusual duplicate-producing behavior if it does fire.
    """
    async with conn.execute("SELECT COUNT(*) FROM price_snapshots") as cursor:
        row = await cursor.fetchone()
    row_count = int(row[0]) if row else 0

    if row_count > 0:
        async with conn.execute("""
            SELECT COUNT(*) - COUNT(DISTINCT
                symbol_base || '|' || symbol_quote || '|' || observed_at)
            FROM price_snapshots
            """) as cursor:
            row = await cursor.fetchone()
        duplicate_count = int(row[0]) if row else 0

        if duplicate_count > 0:
            _LOGGER.warning(
                "price_snapshots: collapsing %d duplicate row(s) before adding UNIQUE index",
                duplicate_count,
                extra={
                    "duplicate_count": duplicate_count,
                    "total_rows": row_count,
                },
            )
            await conn.execute("""
                DELETE FROM price_snapshots
                WHERE snapshot_id NOT IN (
                    SELECT MIN(snapshot_id) FROM price_snapshots
                    GROUP BY symbol_base, symbol_quote, observed_at
                )
                """)

    # Create the index unconditionally; IF NOT EXISTS handles re-runs.
    # Runs against deduplicated data so the constraint applies cleanly
    # even on legacy DBs.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_price_snapshots_unique "
        "ON price_snapshots(symbol_base, symbol_quote, observed_at)"
    )


async def migrate_transfer_results_unique_proposal_id(
    conn: aiosqlite.Connection,
) -> None:
    """DB-enforced idempotency guard for ``cli/harvest --execute`` (ADR-026).

    A partial UNIQUE index -- at most one non-``failed`` ``TransferResult``
    per ``proposal_id``, matching cli/harvest's existing app-layer
    pre-check (a prior ``failed`` row does not block a legitimate
    retry, so the index must not either). This is the "highest-blast-
    radius hole in the codebase" the 2026-06-02 plan review flagged: a
    double-tap, shell re-run, or hang-retry could double-withdraw with
    only the app-layer SELECT-then-INSERT check in the way, which two
    near-simultaneous dispatchers can both pass before either inserts.
    The DB constraint is the concurrency-proof backstop underneath it.

    Unlike ``migrate_price_snapshots_unique``, existing duplicate rows
    here are NOT junk to silently collapse -- a duplicate non-failed
    ``proposal_id`` could be the forensic record of a real past
    double-withdrawal, and deleting one to satisfy the constraint would
    destroy that evidence. If duplicates are found, log loudly and
    leave the index un-created (the guard stays absent, not silently
    "fixed") until an operator investigates and resolves the rows by
    hand -- this fires on every connect until then, which is the point.
    """
    async with conn.execute("""
        SELECT proposal_id, COUNT(*) FROM transfer_results
        WHERE status != 'failed'
        GROUP BY proposal_id
        HAVING COUNT(*) > 1
        """) as cursor:
        duplicates = list(await cursor.fetchall())
    if duplicates:
        _LOGGER.error(
            "transfer_results has %d proposal_id(s) with more than one non-failed "
            "result -- the ADR-026 replay guard cannot be enabled until these are "
            "investigated and resolved by hand (possible past double-withdrawal)",
            len(duplicates),
            extra={"duplicate_proposal_ids": [row[0] for row in duplicates]},
        )
        return
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_results_unique_proposal "
        "ON transfer_results(proposal_id) WHERE status != 'failed'"
    )

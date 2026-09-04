"""``engine_state.offside_since`` migration against a genuinely 2.0-era DB.

The gap this exists to close: ``tests/deployment/test_v1_to_v2_upgrade_
survivor.py`` looks like migration coverage for this column and provides
none. Its fixture reconstructs the real ``v1.0.0`` schema, which has no
``engine_state`` table at all — so ``executescript(SCHEMA)`` creates the
table already carrying ``offside_since`` and ``add_column_if_missing``
no-ops. The ALTER path only exists against a >=2.0 operator.db, which
nothing in ``tests/deployment/`` builds. This file builds one.

The rows matter as much as the column: ``cli/live`` replays operator
pauses out of this table at boot, so a migration that dropped or
corrupted a row would silently resume real trading on a symbol someone
deliberately stopped.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit

# engine_state exactly as 2.0.4 wrote it — no offside_since.
_PRE_SLICE_DDL = """
CREATE TABLE engine_state (
    symbol_base     TEXT NOT NULL CHECK (length(symbol_base) > 0),
    symbol_quote    TEXT NOT NULL CHECK (length(symbol_quote) > 0),
    paused          INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    offside         INTEGER NOT NULL DEFAULT 0 CHECK (offside IN (0, 1)),
    offside_ticks   INTEGER NOT NULL DEFAULT 0 CHECK (offside_ticks >= 0),
    reference_price TEXT,
    anchored_at     TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (symbol_base, symbol_quote)
)
"""

_BTC = Symbol(base="BTC", quote="USD")
_ETH = Symbol(base="ETH", quote="USD")


def _write_pre_slice_db(db_path: Path) -> None:
    """A pre-slice engine_state carrying the production shape: one paused
    symbol and one long-parked offside symbol."""
    conn = sqlite3.connect(str(db_path))
    # WAL before anything else, because the real operator.db has been in WAL
    # since it was created. It matters for the concurrency test below: on a
    # delete-mode DB, `PRAGMA journal_mode = WAL` needs an EXCLUSIVE lock and
    # fails with SQLITE_BUSY when other connections are open, and no busy
    # timeout rescues a journal-mode switch. Seeding in delete mode would test
    # a one-time transition production performed months ago rather than the
    # migration race this file exists for.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(_PRE_SLICE_DDL)
    conn.executemany(
        "INSERT INTO engine_state (symbol_base, symbol_quote, paused, offside, "
        "offside_ticks, reference_price, anchored_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "BTC",
                "USD",
                0,
                1,
                40128,
                "64246.4",
                "2026-08-19T04:06:58+00:00",
                "2026-09-03T23:00:00+00:00",
            ),
            (
                "ETH",
                "USD",
                1,
                1,
                40128,
                "2075.45",
                "2026-08-19T04:06:58+00:00",
                "2026-09-03T23:00:00+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()


async def _open_and_read(db_path: Path) -> list:
    adapter = SQLiteStorageAdapter(str(db_path))
    await adapter.connect()
    try:
        return await adapter.get_engine_states()
    finally:
        await adapter.close()


class TestOffsideSinceMigration:
    def test_column_is_added_and_rows_survive(self, tmp_path: Path) -> None:
        db = tmp_path / "operator.db"
        _write_pre_slice_db(db)
        rows = {r.symbol: r for r in asyncio.run(_open_and_read(db))}
        assert set(rows) == {_BTC, _ETH}
        # The pause is the load-bearing survivor: losing it resumes trading.
        assert rows[_ETH].paused is True
        assert rows[_BTC].offside_ticks == 40128

    def test_existing_rows_land_on_null_not_a_stamped_time(self, tmp_path: Path) -> None:
        """The whole point of the column. Nobody recorded when these
        episodes began, and stamping the migration time — or the next
        boot's — would assert a confident wrong date about symbols parked
        since 2026-08-19."""
        db = tmp_path / "operator.db"
        _write_pre_slice_db(db)
        rows = asyncio.run(_open_and_read(db))
        assert all(r.offside_since is None for r in rows)

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Every daemon in the stack opens this DB; connect() runs the
        migration each time."""
        db = tmp_path / "operator.db"
        _write_pre_slice_db(db)
        asyncio.run(_open_and_read(db))
        rows = asyncio.run(_open_and_read(db))
        assert len(rows) == 2

    def test_a_concurrent_cold_start_does_not_raise(self, tmp_path: Path) -> None:
        """Eight containers open operator.db within ~10s of each other, so
        two can pass add_column_if_missing's PRAGMA check before either
        ALTER commits. The loser must treat "duplicate column" as success —
        the 2026-08-05 outage came from that race, not from a real fault.

        Also covers the plainer hazard measured 2026-09-04: connect() runs
        executescript(SCHEMA) plus every migration in a write transaction,
        and four simultaneous opens exceeded sqlite3's 5s default lock wait.
        """
        db = tmp_path / "operator.db"
        _write_pre_slice_db(db)

        async def _race() -> list[list]:
            return list(await asyncio.gather(*(_open_and_read(db) for _ in range(4))))

        results = asyncio.run(_race())
        assert all(len(r) == 2 for r in results)

    def test_the_schema_is_stable_across_two_passes(self, tmp_path: Path) -> None:
        """A migration that re-adds or re-shapes anything makes the
        deployment gate's byte-identical-schema assertion flap."""
        db = tmp_path / "operator.db"
        _write_pre_slice_db(db)
        asyncio.run(_open_and_read(db))
        conn = sqlite3.connect(str(db))
        first = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        conn.close()
        asyncio.run(_open_and_read(db))
        conn = sqlite3.connect(str(db))
        second = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        assert first == second
        assert integrity == "ok"

    def test_a_fresh_db_has_the_column_without_the_alter(self, tmp_path: Path) -> None:
        """SCHEMA declares it, so the ALTER no-ops on a new install — the
        exact reason the v1-to-v2 deployment gate cannot cover this."""
        db = tmp_path / "fresh.db"
        asyncio.run(_open_and_read(db))
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(engine_state)")}
        conn.close()
        assert "offside_since" in cols

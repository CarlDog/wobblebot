"""Real tagged schema/writer compatibility, including downgrade then re-upgrade."""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wobblebot.adapters import sqlite_storage
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _tagged(path):
    try:
        return subprocess.run(
            ["git", "show", f"v2.0.7:{path}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        message = f"2.0.7 compatibility gate requires the v2.0.7 tag: {type(exc).__name__}"
        if os.environ.get("WOBBLEBOT_REQUIRE_UPGRADE_GATE") == "1":
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture(scope="module")
def legacy():
    schema_tree = ast.parse(_tagged("src/wobblebot/adapters/sqlite_storage_schema.py"))
    schema = next(
        ast.literal_eval(n.value)
        for n in schema_tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets)
    )
    tree = ast.parse(_tagged("src/wobblebot/adapters/sqlite_storage.py"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SQLiteStorageAdapter"
    )
    methods = [
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name in {"save_engine_state", "get_engine_states"}
    ]
    assert len(methods) == 2
    namespace = dict(vars(sqlite_storage))
    # Trusted tagged methods, with their unchanged helper imports. Execute the
    # real prior SQL and row mapper, not an approximation of its upsert.
    exec(
        compile(ast.Module(body=methods, type_ignores=[]), "<v2.0.7-engine-state>", "exec"),
        namespace,
    )
    return schema, namespace["save_engine_state"], namespace["get_engine_states"]


def _seed(path, schema):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema)
        conn.execute("""INSERT INTO engine_state
            (symbol_base, symbol_quote, paused, offside, offside_ticks, offside_since, updated_at)
            VALUES ('BTC', 'USD', 1, 1, 123, '2026-08-19T00:00:00+00:00',
                    '2026-09-01T00:00:00+00:00')""")
        conn.commit()


@pytest.mark.asyncio
async def test_tagged_schema_migration_repeatability_and_backup_restore(tmp_path, legacy):
    path = tmp_path / "operator.db"
    backup = tmp_path / "before-upgrade.db"
    _seed(path, legacy[0])
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    schemas = []
    for _ in range(2):
        storage = SQLiteStorageAdapter(str(path))
        await storage.connect()
        try:
            [row] = await storage.get_engine_states()
            assert row.paused and row.offside and row.offside_ticks == 123
            assert row.offside_since == datetime(2026, 8, 19, tzinfo=UTC)
            assert row.starved_ticks == 0 and row.starved_reasons == {}
        finally:
            await storage.close()
        with closing(sqlite3.connect(path)) as conn:
            schemas.append(
                conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            )
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert schemas[0] == schemas[1]
    # Restore only a disposable fixture. A live backup must never erase later effects.
    restored = tmp_path / "restored.db"
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
    with closing(sqlite3.connect(restored)) as conn:
        assert "starved_ticks" not in {
            r[1] for r in conn.execute("PRAGMA table_info(engine_state)")
        }
        assert conn.execute("SELECT paused, offside_ticks FROM engine_state").fetchone() == (1, 123)


@pytest.mark.asyncio
async def test_old_writer_insert_and_upsert_cannot_refresh_stale_diagnostics(tmp_path, legacy):
    path = tmp_path / "operator.db"
    _seed(path, legacy[0])
    _, old_write, old_read = legacy
    storage = SQLiteStorageAdapter(str(path))
    await storage.connect()
    try:
        [original] = await storage.get_engine_states()
        starved = replace(
            original,
            paused=False,
            offside=False,
            offside_ticks=0,
            starved_ticks=60,
            starved_target=6,
            starved_refusals=6,
            starved_reasons={"insufficient_balance": 6},
        )
        await storage.save_engine_state(starved)
        assert (await old_read(storage))[0].symbol == starved.symbol
        # The old upsert advances only columns it knows, retaining starvation.
        updated = replace(starved, paused=True, updated_at=starved.updated_at + timedelta(days=1))
        await old_write(storage, updated)
        inserted = replace(original, symbol=Symbol(base="ETH", quote="USD"))
        await old_write(storage, inserted)
        rows = {r.symbol: r for r in await storage.get_engine_states()}
        assert rows[starved.symbol].updated_at == updated.updated_at
        assert rows[starved.symbol].paused
        assert all(r.starved_ticks == 0 and r.starved_reasons == {} for r in rows.values())
        raw = await storage._require_conn().execute_fetchall(
            "SELECT starved_ticks FROM engine_state WHERE symbol_base='BTC'"
        )
        assert raw[0][0] == 60  # Suppression is real; the old upsert did not clear it.
    finally:
        await storage.close()
    # Re-upgrade/reconnect cannot bless a retained diagnostic either.
    await storage.connect()
    try:
        rows = {r.symbol: r for r in await storage.get_engine_states()}
        assert rows[starved.symbol].starved_ticks == 0
        fresh = replace(starved, updated_at=updated.updated_at + timedelta(seconds=5))
        await storage.save_engine_state(fresh)
        rows = {r.symbol: r for r in await storage.get_engine_states()}
        assert rows[starved.symbol] == fresh
    finally:
        await storage.close()

"""Cloud-provider migration against tagged schema, including failure rollback."""

import ast
import asyncio
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal

import aiosqlite
import pytest

from wobblebot.adapters.sqlite_llm_provider_migration import migrate_llm_calls_ollama_cloud
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def legacy_db(tmp_path):
    tagged = subprocess.run(
        ["git", "show", "v2.0.9:src/wobblebot/adapters/sqlite_storage_schema.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    syntax = ast.parse(tagged.stdout)
    schema = next(
        ast.literal_eval(node.value)
        for node in syntax.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets)
    )
    path = tmp_path / "cloud-upgrade.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE call_audit (id TEXT)")
        conn.execute(
            "CREATE TRIGGER cloud_test_audit AFTER INSERT ON llm_calls "
            "BEGIN INSERT INTO call_audit VALUES (new.id); END"
        )
        conn.execute("CREATE INDEX cloud_test_index ON llm_calls(request_id)")
    old_writer(path, "old-call")
    return path


def old_writer(path, identity):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO llm_calls (id,timestamp,role,provider,model,tokens_in,tokens_out,"
            "tokens_reasoning,tokens_cache_read,tokens_cache_write,cost_usd,request_id,"
            "success,error_kind,trace_id) VALUES (?,?,'news','openai','gpt-5-mini',100,20,"
            "10,5,0,'0.012345','old-request',1,NULL,'old-trace')",
            (identity, datetime.now(UTC).isoformat()),
        )
        conn.commit()


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()


async def test_upgrade_preserves_history_indexes_triggers_and_prior_writer(legacy_db):
    before = snapshot(legacy_db)
    readonly = SQLiteStorageAdapter(legacy_db, read_only=True)
    await readonly.connect()
    await readonly.close()
    with closing(sqlite3.connect(legacy_db)) as conn:
        assert (
            "ollama_cloud"
            not in conn.execute("SELECT sql FROM sqlite_master WHERE name='llm_calls'").fetchone()[
                0
            ]
        )
    adapter = SQLiteStorageAdapter(legacy_db)
    await adapter.connect()
    try:
        assert snapshot(legacy_db) == before
        record = LLMCallRecord(
            timestamp=Timestamp(dt=datetime.now(UTC)),
            provider="ollama_cloud",
            model="gpt-oss:120b",
            role="news",
            tokens_in=800,
            tokens_out=500,
            tokens_cache_read=200,
            cost_usd=Decimal("0.000423"),
            success=True,
            trace_id="cloud-trace",
        )
        await adapter.save_llm_call(record)
        assert (await adapter.get_llm_calls(provider="ollama_cloud"))[0] == record
    finally:
        await adapter.close()
    old_writer(legacy_db, "old-writer-after")
    after = snapshot(legacy_db)
    await adapter.connect()
    await adapter.close()
    assert snapshot(legacy_db) == after
    with closing(sqlite3.connect(legacy_db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(llm_calls)")}
        assert {
            "cloud_test_index",
            "idx_llm_calls_role",
            "idx_llm_calls_timestamp",
            "idx_llm_calls_provider_model",
        } <= indexes
        assert len(conn.execute("SELECT * FROM call_audit").fetchall()) == 3


async def test_concurrent_upgraders_are_idempotent(legacy_db):
    adapters = [SQLiteStorageAdapter(legacy_db) for _ in range(4)]
    before = snapshot(legacy_db)
    try:
        await asyncio.wait_for(asyncio.gather(*(adapter.connect() for adapter in adapters)), 35)
        assert snapshot(legacy_db) == before
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_failure_rolls_back_entire_table_rebuild(legacy_db, monkeypatch):
    before = snapshot(legacy_db)
    async with aiosqlite.connect(legacy_db) as conn:
        execute = conn.execute

        def fail_rename(sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE llm_calls_ollama_cloud"):
                raise aiosqlite.OperationalError("injected rename failure")
            return execute(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", fail_rename)
        with pytest.raises(aiosqlite.OperationalError, match="injected"):
            await migrate_llm_calls_ollama_cloud(conn)
    assert snapshot(legacy_db) == before
    with closing(sqlite3.connect(legacy_db)) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='llm_calls_ollama_cloud'"
            ).fetchall()
            == []
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

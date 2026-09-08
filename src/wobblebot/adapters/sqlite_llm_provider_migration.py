"""Widen the legacy cloud-provider CHECK without losing forensic ledger data."""

from __future__ import annotations

import re

import aiosqlite

_OLD_PROVIDERS = re.compile(r"'anthropic'\s*,\s*'openai'\s*,\s*'google'")


async def migrate_llm_calls_ollama_cloud(conn: aiosqlite.Connection) -> None:
    """Transactional, repeatable rebuild; retain column order, indexes and triggers.

    SQLite cannot widen a CHECK with ALTER COLUMN. A write lock before inspecting
    the schema serializes concurrent daemon starts. A savepoint also protects
    callers already in a transaction; do not use executescript (implicit commit).
    The original DDL is retained except for the provider allowlist and table name.
    """
    await conn.execute("SAVEPOINT llm_provider_upgrade")
    try:
        await conn.execute("UPDATE llm_calls SET id = id WHERE 0")
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_calls'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        original = str(row[0])
        # Some legacy/imported ledgers have no CHECK constraints at all; their
        # provider column already accepts Cloud and needs no rebuild.
        if "'ollama_cloud'" not in original and re.search(r"\bCHECK\s*\(", original, re.I):
            updated, count = _OLD_PROVIDERS.subn(
                "'anthropic', 'openai', 'google', 'ollama_cloud'", original
            )
            if count != 1:
                raise aiosqlite.OperationalError("Unrecognized llm_calls provider constraint")
            updated, count = re.subn(
                r'CREATE TABLE (?:IF NOT EXISTS )?["`\[]?llm_calls["`\]]?',
                "CREATE TABLE llm_calls_ollama_cloud",
                updated,
                count=1,
                flags=re.IGNORECASE,
            )
            if count != 1:
                raise aiosqlite.OperationalError("Unrecognized llm_calls table definition")
            async with conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'llm_calls' "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            ) as cursor:
                dependents = [str(item[0]) async for item in cursor]
            await conn.execute(updated)
            await conn.execute("INSERT INTO llm_calls_ollama_cloud SELECT * FROM llm_calls")
            await conn.execute("DROP TABLE llm_calls")
            await conn.execute("ALTER TABLE llm_calls_ollama_cloud RENAME TO llm_calls")
            for ddl in dependents:
                await conn.execute(ddl)
        await conn.execute("RELEASE SAVEPOINT llm_provider_upgrade")
    except BaseException:
        await conn.execute("ROLLBACK TO SAVEPOINT llm_provider_upgrade")
        await conn.execute("RELEASE SAVEPOINT llm_provider_upgrade")
        raise

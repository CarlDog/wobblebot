"""Upgrade the tagged 2.0.9 schema and preserve prior-writer compatibility."""

import ast
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion, LLMAdvisorAttempt

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _old_writer(db, identity: str) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            """INSERT INTO advisor_suggestions
            (recommendation_id, created_at, role, recommendations, rationale, confidence,
             input_summary, model_name, expert_opinions, news_materially_drove)
            VALUES (?, ?, 'news', '{}', 'original rationale', 'low', '{}', 'primary', '[]', 0)""",
            (identity, datetime.now(UTC).isoformat()),
        )
        conn.commit()


async def test_tagged_schema_readonly_upgrade_and_prior_writer_survive(tmp_path) -> None:
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
    db = tmp_path / "upgrade.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(schema)
    _old_writer(db, "before")
    readonly = SQLiteStorageAdapter(db, read_only=True)
    await readonly.connect()
    try:
        assert (await readonly.get_advisor_suggestions())[0].recommendation.llm_attempts == []
    finally:
        await readonly.close()
    with closing(sqlite3.connect(db)) as conn:
        assert "llm_attempts" not in {
            row[1] for row in conn.execute("PRAGMA table_info(advisor_suggestions)")
        }

    adapter = SQLiteStorageAdapter(db)
    await adapter.connect()
    attempts = [
        LLMAdvisorAttempt(
            role="news", provider="anthropic", model="primary", error_kind="insufficient_credit"
        ),
        LLMAdvisorAttempt(role="news", provider="openai", model="backup"),
    ]
    now = Timestamp(dt=datetime.now(UTC))
    try:
        await adapter.save_advisor_suggestion(
            AdvisorSuggestion(
                recommendation=AdvisorRecommendation(
                    recommendation_id="new",
                    timestamp=now,
                    role="news",
                    rationale="new rationale",
                    confidence="high",
                    llm_attempts=attempts,
                ),
                created_at=now,
                input_summary={},
                model_name="openai/backup",
            )
        )
        _old_writer(db, "after")
    finally:
        await adapter.close()
    await adapter.connect()  # Idempotent migration and on-disk readback.
    try:
        rows = {
            row.recommendation.recommendation_id: row
            for row in await adapter.get_advisor_suggestions()
        }
        assert len(rows) == 3
        assert rows["before"].recommendation.llm_attempts == []
        assert rows["after"].recommendation.llm_attempts == []
        assert rows["before"].recommendation.rationale == "original rationale"
        assert rows["new"].recommendation.llm_attempts == attempts
    finally:
        await adapter.close()

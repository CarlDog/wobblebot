"""SQLiteStorageAdapter tests for the outcome ledger (ADR-035, P4.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    AdvisorSuggestion,
    RecommendationOutcome,
)
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _suggestion(role: str = "quant") -> AdvisorSuggestion:
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=_NOW),
        role=role,  # type: ignore[arg-type]
        recommendations={"spacing_percentage": 1.2},
        rationale="test",
        confidence="medium",
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=_NOW),
        input_summary={"symbol": "BTC/USD", "current_grid": {"spacing_percentage": 3.0}},
        model_name="gpt-5-mini",
    )


def _outcome(
    suggestion_id: int,
    *,
    granularity: int | None = 60,
    version: int = 1,
    scoreable: bool = True,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        suggestion_id=suggestion_id,
        kind="config_rec",
        scoreable=scoreable,
        unscoreable_reason=None if scoreable else "missing current_grid",
        window_start=Timestamp(dt=_NOW),
        window_end=Timestamp(dt=_NOW + timedelta(days=7)),
        granularity_minutes=granularity,
        proposed_arm_json={"cycles": 2} if scoreable else None,
        inforce_arm_json={"cycles": 1} if scoreable else None,
        outcome="better" if scoreable else None,
        evaluator_version=version,
        scored_at=Timestamp(dt=_NOW),
    )


async def test_round_trip(storage: SQLiteStorageAdapter) -> None:
    await storage.save_advisor_suggestion(_suggestion())
    row_id = await storage.save_recommendation_outcome(_outcome(1))
    assert row_id == 1
    rows = await storage.get_recommendation_outcomes(suggestion_id=1)
    assert len(rows) == 1
    got = rows[0]
    assert got.kind == "config_rec"
    assert got.outcome == "better"
    assert got.proposed_arm_json == {"cycles": 2}
    assert got.granularity_minutes == 60
    assert (got.window_end.dt - got.window_start.dt) == timedelta(days=7)


async def test_unique_blocks_duplicate_scoring(storage: SQLiteStorageAdapter) -> None:
    """Idempotence contract: same (suggestion, granularity, version)
    cannot be scored twice; a new evaluator version appends instead."""
    await storage.save_advisor_suggestion(_suggestion())
    await storage.save_recommendation_outcome(_outcome(1))
    with pytest.raises(StorageError):
        await storage.save_recommendation_outcome(_outcome(1))
    # New version is a NEW row, old row untouched.
    await storage.save_recommendation_outcome(_outcome(1, version=2))
    assert len(await storage.get_recommendation_outcomes(suggestion_id=1)) == 2


async def test_unscoreable_row_carries_reason(storage: SQLiteStorageAdapter) -> None:
    await storage.save_advisor_suggestion(_suggestion())
    await storage.save_recommendation_outcome(_outcome(1, scoreable=False))
    rows = await storage.get_recommendation_outcomes(scoreable=False)
    assert len(rows) == 1
    assert rows[0].unscoreable_reason == "missing current_grid"
    assert rows[0].outcome is None


async def test_unscored_queue_shrinks_as_scores_land(storage: SQLiteStorageAdapter) -> None:
    for _ in range(3):
        await storage.save_advisor_suggestion(_suggestion())
    queue = await storage.get_unscored_suggestions(60, 1)
    assert [row_id for row_id, _ in queue] == [1, 2, 3]  # oldest first
    await storage.save_recommendation_outcome(_outcome(2))
    queue = await storage.get_unscored_suggestions(60, 1)
    assert [row_id for row_id, _ in queue] == [1, 3]
    # A different granularity has its own independent queue.
    assert len(await storage.get_unscored_suggestions(1, 1)) == 3
    # And so does a different evaluator version.
    assert len(await storage.get_unscored_suggestions(60, 2)) == 3


async def test_unscored_queue_null_granularity(storage: SQLiteStorageAdapter) -> None:
    """Directional calls score at NULL granularity — IS-comparison, not =."""
    await storage.save_advisor_suggestion(_suggestion())
    await storage.save_recommendation_outcome(_outcome(1, granularity=None))
    assert await storage.get_unscored_suggestions(None, 1) == []
    assert len(await storage.get_unscored_suggestions(60, 1)) == 1


async def test_unscored_returns_reconstructed_suggestion(
    storage: SQLiteStorageAdapter,
) -> None:
    await storage.save_advisor_suggestion(_suggestion(role="quant"))
    [(row_id, suggestion)] = await storage.get_unscored_suggestions(60, 1)
    assert row_id == 1
    assert suggestion.recommendation.role == "quant"
    assert suggestion.input_summary["current_grid"] == {"spacing_percentage": 3.0}


async def test_get_advisor_suggestions_by_ids(storage: SQLiteStorageAdapter) -> None:
    """The P4.3 scoreboard's join primitive: keyed reads, misses absent."""
    for _ in range(3):
        await storage.save_advisor_suggestion(_suggestion())
    rows = await storage.get_advisor_suggestions_by_ids([3, 1])
    assert sorted(row_id for row_id, _ in rows) == [1, 3]
    assert all(s.recommendation.role == "quant" for _, s in rows)
    assert await storage.get_advisor_suggestions_by_ids([]) == []
    # A missing id is a domain-data miss, not an error.
    assert await storage.get_advisor_suggestions_by_ids([99]) == []


async def test_llm_calls_trace_id_column_exists(storage: SQLiteStorageAdapter) -> None:
    """The per-cycle-tracing half of the P4.1 migration."""
    conn = storage._require_conn()  # pylint: disable=protected-access
    async with conn.execute("PRAGMA table_info(llm_calls)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    assert "trace_id" in columns

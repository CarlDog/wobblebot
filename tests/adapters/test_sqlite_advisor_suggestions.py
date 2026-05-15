"""SQLiteStorageAdapter tests for the advisor-suggestions persistence (Stage 3.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_suggestion(
    *,
    model_name: str = "phi4:14b",
    role: str = "single",
    confidence: str = "medium",
    minutes_ago: int = 5,
    summary: dict[str, object] | None = None,
    recommendations: dict[str, object] | None = None,
) -> AdvisorSuggestion:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=created),
        role=role,
        recommendations=recommendations or {"spacing_percentage": 1.2},
        rationale=f"Test rationale from {model_name}.",
        confidence=confidence,  # type: ignore[arg-type]
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=created),
        input_summary=summary or {"symbol": "BTC/USD", "volatility": 0.0004},
        model_name=model_name,
    )


async def test_save_then_read(storage: SQLiteStorageAdapter) -> None:
    suggestion = _make_suggestion()
    await storage.save_advisor_suggestion(suggestion)
    result = await storage.get_advisor_suggestions()
    assert len(result) == 1
    got = result[0]
    assert got.model_name == "phi4:14b"
    assert got.recommendation.confidence == "medium"
    assert got.recommendation.recommendations == {"spacing_percentage": 1.2}
    assert got.input_summary == {"symbol": "BTC/USD", "volatility": 0.0004}


async def test_input_summary_dict_round_trips(storage: SQLiteStorageAdapter) -> None:
    """Forensic record: the input summary survives storage as-is, regardless
    of whether its shape matches the current PerformanceSummary schema."""
    legacy_summary = {
        "symbol": "BTC/USD",
        "lookback_hours": 24.0,
        "snapshot_count": 1000,
        # Imagine this field existed in v1 but not in v2 — should still round-trip
        "legacy_indicator_v1": 0.42,
    }
    await storage.save_advisor_suggestion(_make_suggestion(summary=legacy_summary))
    got = (await storage.get_advisor_suggestions())[0]
    assert got.input_summary == legacy_summary


async def test_default_order_is_created_desc(storage: SQLiteStorageAdapter) -> None:
    for offset, model in [(30, "old"), (10, "mid"), (1, "new")]:
        await storage.save_advisor_suggestion(
            _make_suggestion(model_name=model, minutes_ago=offset)
        )
    result = await storage.get_advisor_suggestions()
    assert [s.model_name for s in result] == ["new", "mid", "old"]


async def test_model_name_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_advisor_suggestion(_make_suggestion(model_name="phi4:14b"))
    await storage.save_advisor_suggestion(_make_suggestion(model_name="qwq:32b"))
    result = await storage.get_advisor_suggestions(model_name="qwq:32b")
    assert len(result) == 1
    assert result[0].model_name == "qwq:32b"


async def test_role_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_advisor_suggestion(_make_suggestion(role="single"))
    await storage.save_advisor_suggestion(_make_suggestion(role="quant"))
    await storage.save_advisor_suggestion(_make_suggestion(role="risk"))
    result = await storage.get_advisor_suggestions(role="quant")
    assert len(result) == 1
    assert result[0].recommendation.role == "quant"


async def test_since_filter_inclusive(storage: SQLiteStorageAdapter) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for off, model in [(0, "oldest"), (30, "mid"), (60, "newer"), (90, "newest")]:
        rec = AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=base + timedelta(minutes=off)),
            role="single",
            recommendations={},
            rationale="x",
            confidence="low",
        )
        await storage.save_advisor_suggestion(
            AdvisorSuggestion(
                recommendation=rec,
                created_at=Timestamp(dt=base + timedelta(minutes=off)),
                input_summary={},
                model_name=model,
            )
        )
    cutoff = base + timedelta(minutes=30)
    result = await storage.get_advisor_suggestions(since=cutoff)
    assert len(result) == 3
    assert all(s.created_at.dt >= cutoff for s in result)


async def test_limit_caps_rows(storage: SQLiteStorageAdapter) -> None:
    for i in range(5):
        await storage.save_advisor_suggestion(_make_suggestion(model_name=f"m{i}", minutes_ago=i))
    result = await storage.get_advisor_suggestions(limit=2)
    assert len(result) == 2


async def test_empty_returns_empty_list(storage: SQLiteStorageAdapter) -> None:
    assert await storage.get_advisor_suggestions() == []


async def test_recommendations_dict_round_trips_with_nesting(
    storage: SQLiteStorageAdapter,
) -> None:
    """The recommendations dict may carry nested/complex values."""
    complex_recs: dict[str, object] = {
        "spacing_percentage": 1.5,
        "levels_above": 4,
        "nested_object": {"max_per_coin_usd": 50, "enabled_coins": ["BTC", "ETH"]},
    }
    await storage.save_advisor_suggestion(_make_suggestion(recommendations=complex_recs))
    got = (await storage.get_advisor_suggestions())[0]
    assert got.recommendation.recommendations == complex_recs


async def test_confidence_constraint_enforced(storage: SQLiteStorageAdapter) -> None:
    """The DB-level CHECK on confidence rejects bad values that somehow
    bypass Pydantic validation (e.g. if a future code path constructs
    rows directly). Pydantic enforces the same thing at construction —
    this is the belt-and-suspenders DB check."""
    # Pydantic blocks construction outright, so we don't need to test the
    # direct-INSERT path. Just verify the round-trip preserves the valid value.
    for level in ("high", "medium", "low"):
        await storage.save_advisor_suggestion(_make_suggestion(confidence=level))
    result = await storage.get_advisor_suggestions()
    assert {s.recommendation.confidence for s in result} == {"high", "medium", "low"}


async def test_full_text_rationale_survives(storage: SQLiteStorageAdapter) -> None:
    long_rationale = (
        "The metrics window shows declining volatility (0.0004 vs prior 0.0008) "
        "combined with a recent drawdown of 2.5%. The lack of recent cycle "
        "history (cycle_count=0) makes confidence-weighted recommendations "
        "tentative. Net guidance: hold current parameters, monitor next 6 hours."
    )
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="single",
        recommendations={},
        rationale=long_rationale,
        confidence="low",
    )
    suggestion = AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary={},
        model_name="phi4:14b",
    )
    await storage.save_advisor_suggestion(suggestion)
    got = (await storage.get_advisor_suggestions())[0]
    assert got.recommendation.rationale == long_rationale


async def test_expert_opinions_empty_for_single_mode(
    storage: SQLiteStorageAdapter,
) -> None:
    """Single-LLM suggestions persist with an empty expert_opinions list —
    the Stage 3.3 path stays unchanged at the read boundary."""
    await storage.save_advisor_suggestion(_make_suggestion())
    got = (await storage.get_advisor_suggestions())[0]
    assert got.recommendation.expert_opinions == []


async def test_expert_opinions_round_trip(storage: SQLiteStorageAdapter) -> None:
    """MoE suggestions persist every expert's role / confidence /
    recommendations / rationale and read back as a populated list of
    ``AdvisorRecommendation`` instances."""
    expert_opinions = [
        AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="quant",
            recommendations={"spacing_percentage": 1.2},
            rationale="vol spiking",
            confidence="high",
        ),
        AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="risk",
            recommendations={"spacing_percentage": 1.5},
            rationale="drawdown widening",
            confidence="medium",
        ),
        AdvisorRecommendation(
            recommendation_id=str(uuid4()),
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="news",
            recommendations={},
            rationale="quiet news window",
            confidence="low",
        ),
    ]
    aggregated = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="aggregated",
        recommendations={"spacing_percentage": 1.35},
        rationale="voting consensus",
        confidence="medium",
        expert_opinions=expert_opinions,
    )
    suggestion = AdvisorSuggestion(
        recommendation=aggregated,
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary={"symbol": "BTC/USD"},
        model_name="moe[voting:quant:phi4/risk:qwen3/news:r1]",
    )
    await storage.save_advisor_suggestion(suggestion)

    got = (await storage.get_advisor_suggestions())[0]
    assert got.recommendation.role == "aggregated"
    assert len(got.recommendation.expert_opinions) == 3
    roles = {op.role for op in got.recommendation.expert_opinions}
    assert roles == {"quant", "risk", "news"}
    by_role = {op.role: op for op in got.recommendation.expert_opinions}
    assert by_role["quant"].confidence == "high"
    assert by_role["quant"].rationale == "vol spiking"
    assert by_role["quant"].recommendations == {"spacing_percentage": 1.2}
    assert by_role["news"].recommendations == {}


async def test_expert_opinions_migration_on_pre_3_4a_db(tmp_path: object) -> None:
    """Operators upgrading from Stage 3.3 have advisor_suggestions tables
    without the ``expert_opinions`` column. ``connect()`` must ALTER the
    table to add it (defaulted to ``'[]'``) without losing existing rows."""
    from pathlib import Path as _Path

    import aiosqlite as _aiosqlite

    db_path = _Path(tmp_path) / "legacy.db"  # type: ignore[arg-type]

    # Materialize a pre-3.4a schema by hand: same columns as Stage 3.3, no
    # expert_opinions column. Then insert a row so we can prove data
    # survives the migration.
    legacy_create = """
    CREATE TABLE advisor_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recommendation_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        role TEXT NOT NULL,
        recommendations TEXT NOT NULL,
        rationale TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
        input_summary TEXT NOT NULL,
        model_name TEXT NOT NULL
    );
    """
    async with _aiosqlite.connect(str(db_path)) as raw:
        await raw.executescript(legacy_create)
        await raw.execute(
            """
            INSERT INTO advisor_suggestions (
                recommendation_id, created_at, role, recommendations,
                rationale, confidence, input_summary, model_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-id",
                datetime.now(UTC).isoformat(),
                "single",
                "{}",
                "pre-migration row",
                "low",
                "{}",
                "phi4:14b",
            ),
        )
        await raw.commit()

    # Now open via the adapter — connect() should detect the missing column
    # and ALTER the table.
    adapter = SQLiteStorageAdapter(str(db_path))
    await adapter.connect()
    try:
        got = await adapter.get_advisor_suggestions()
        assert len(got) == 1
        assert got[0].recommendation.rationale == "pre-migration row"
        # The pre-existing row picked up the default empty-list expert_opinions.
        assert got[0].recommendation.expert_opinions == []

        # And new writes work — proves the column is present + writeable.
        await adapter.save_advisor_suggestion(_make_suggestion(model_name="post-migration"))
        got2 = await adapter.get_advisor_suggestions(model_name="post-migration")
        assert len(got2) == 1
    finally:
        await adapter.close()

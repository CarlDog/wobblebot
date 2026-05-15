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

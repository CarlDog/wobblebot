"""End-to-end tests for tools/score_recommendations.py (ADR-035, P4.2).

Synthetic market: 60m bars alternating 100 / 98 for the full 7-day
window. A 1.8%-spacing grid buys the 98 dips and sells the 100 crests
(profitable cycles under the pre-cutover 0.25% maker fee); a 3.0% grid
never fills — its first buy sits at 97. So a suggestion proposing 1.8
against an in-force 3.0 must score ``better`` deterministically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from tools.score_recommendations import score_corpus
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.value_objects import OHLCBar, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion
from wobblebot.services.advisor_evaluator import REPLAY_WINDOW

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(days=8)  # every _T0 window has fully elapsed
_BTC = Symbol.from_string("BTC/USD")

# Generous inventory caps so ADR-039 headroom is not what this test
# measures; the order-book caps are roomy for the same reason.
_SAFETY = SafetyConfig(
    max_total_exposure_usd=Decimal("1000"),
    max_daily_spend_usd=Decimal("1000"),
    max_per_coin_exposure_usd=Decimal("500"),
    max_orders_per_coin=12,
    max_per_coin_inventory_usd=Decimal("500"),
    max_total_inventory_usd=Decimal("1000"),
)


@pytest_asyncio.fixture
async def advise() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def bars_db() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _suggestion(
    recommendations: dict[str, Any],
    *,
    symbol: str = "BTC/USD",
    created: datetime = _T0,
) -> AdvisorSuggestion:
    rec = AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=created),
        role="quant",
        recommendations=recommendations,
        rationale="test",
        confidence="medium",
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=created),
        input_summary={
            "symbol": symbol,
            "current_grid": {
                "spacing_percentage": 3.0,
                "levels_above": 3,
                "levels_below": 3,
                "order_size_usd": 10.0,
            },
        },
        model_name="gpt-5-mini",
    )


def _oscillation_bars(symbol: Symbol = _BTC) -> list[OHLCBar]:
    bars = []
    count = int(REPLAY_WINDOW / timedelta(minutes=60))
    for i in range(count):
        price = Decimal("100") if i % 2 == 0 else Decimal("98")
        bars.append(
            OHLCBar(
                symbol=symbol,
                interval_minutes=60,
                opened_at=_T0 + timedelta(hours=i),
                open=price,
                high=price,
                low=price,
                close=price,
                vwap=price,
                volume=Decimal("1"),
                count=1,
            )
        )
    return bars


async def test_scores_better_when_proposed_cycles(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(_suggestion({"spacing_percentage": 1.8}))
    await bars_db.save_ohlc_bars(_oscillation_bars())
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert stats.scored["better"] == 1
    assert stats.processed == 1
    [row] = await advise.get_recommendation_outcomes(suggestion_id=1)
    assert row.scoreable is True
    assert row.outcome == "better"
    assert row.granularity_minutes == 60
    assert row.proposed_arm_json is not None and row.inforce_arm_json is not None
    assert row.proposed_arm_json["config"]["spacing_percentage"] == 1.8
    assert row.inforce_arm_json["config"]["spacing_percentage"] == 3.0
    assert row.proposed_arm_json["cycle_count"] > 0
    assert row.inforce_arm_json["cycle_count"] == 0
    # _T0 predates the 2026-07-09 fee doubling: the window replays at
    # the retired 0.25% maker rate, pinning the schedule wiring.
    assert row.proposed_arm_json["fee_rate"] == 0.0025


async def test_equal_proposal_scores_tie(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(_suggestion({"spacing_percentage": 3.0}))
    await bars_db.save_ohlc_bars(_oscillation_bars())
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert stats.scored["tie"] == 1
    [row] = await advise.get_recommendation_outcomes(suggestion_id=1)
    assert row.outcome == "tie"


async def test_empty_rec_recorded_unscoreable(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(_suggestion({}))
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert sum(stats.unscoreable.values()) == 1
    [row] = await advise.get_recommendation_outcomes(suggestion_id=1)
    assert row.scoreable is False
    assert row.unscoreable_reason is not None
    assert "empty recommendation" in row.unscoreable_reason


async def test_missing_bars_left_in_queue(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    """Bar absence is a fact about this machine's imports, not the
    suggestion — no row is written, so importing the dump and
    re-running scores it at the SAME evaluator version."""
    await advise.save_advisor_suggestion(_suggestion({"spacing_percentage": 1.8}))
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert stats.bars_missing == 1
    assert stats.processed == 0
    assert await advise.get_recommendation_outcomes() == []
    assert len(await advise.get_unscored_suggestions(60, 1)) == 1


async def test_pending_window_left_in_queue(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(
        _suggestion({"spacing_percentage": 1.8}, created=_NOW - timedelta(days=1))
    )
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert stats.pending == 1
    assert stats.processed == 0
    assert await advise.get_recommendation_outcomes() == []
    assert len(await advise.get_unscored_suggestions(60, 1)) == 1


async def test_symbol_filter_skips_other_symbols(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(_suggestion({"spacing_percentage": 1.8}, symbol="ETH/USD"))
    stats = await score_corpus(
        advise,
        bars_db,
        interval_minutes=60,
        safety_config=_SAFETY,
        symbols={_BTC},
        now=_NOW,
    )
    assert stats.filtered == 1
    assert stats.processed == 0
    assert await advise.get_recommendation_outcomes() == []


async def test_limit_caps_written_rows(
    advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter
) -> None:
    await advise.save_advisor_suggestion(_suggestion({}))
    await advise.save_advisor_suggestion(_suggestion({}))
    stats = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, limit=1, now=_NOW
    )
    assert stats.processed == 1
    assert len(await advise.get_unscored_suggestions(60, 1)) == 1


async def test_rerun_is_noop(advise: SQLiteStorageAdapter, bars_db: SQLiteStorageAdapter) -> None:
    await advise.save_advisor_suggestion(_suggestion({"spacing_percentage": 1.8}))
    await bars_db.save_ohlc_bars(_oscillation_bars())
    first = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert first.processed == 1
    second = await score_corpus(
        advise, bars_db, interval_minutes=60, safety_config=_SAFETY, now=_NOW
    )
    assert second.processed == 0
    assert len(await advise.get_recommendation_outcomes(suggestion_id=1)) == 1

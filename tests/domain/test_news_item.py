"""Unit tests for the NewsItem domain model (Stage 3.2.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp

pytestmark = pytest.mark.unit


def _make(**overrides: object) -> NewsItem:
    base: dict[str, object] = {
        "source": "rss:coindesk",
        "external_id": "abc",
        "published_at": Timestamp(dt=datetime.now(UTC)),
        "headline": "BTC moves",
    }
    base.update(overrides)
    return NewsItem(**base)  # type: ignore[arg-type]


def test_minimal_valid() -> None:
    item = _make()
    assert item.body == ""
    assert item.sentiment_score is None
    assert item.mentioned_coins == []
    assert item.fetched_at.dt.tzinfo is not None


def test_frozen() -> None:
    item = _make()
    with pytest.raises(ValidationError):
        item.headline = "edited"  # type: ignore[misc]


def test_external_id_optional() -> None:
    item = _make(external_id=None)
    assert item.external_id is None


def test_headline_required_nonempty() -> None:
    with pytest.raises(ValidationError):
        _make(headline="")


def test_source_required_nonempty() -> None:
    with pytest.raises(ValidationError):
        _make(source="")


def test_sentiment_clamped_to_unit_interval() -> None:
    with pytest.raises(ValidationError):
        _make(sentiment_score=1.5)
    with pytest.raises(ValidationError):
        _make(sentiment_score=-1.5)


def test_sentiment_bounds_inclusive() -> None:
    assert _make(sentiment_score=1.0).sentiment_score == 1.0
    assert _make(sentiment_score=-1.0).sentiment_score == -1.0
    assert _make(sentiment_score=0.0).sentiment_score == 0.0


def test_mentioned_coins_accepts_list() -> None:
    item = _make(mentioned_coins=["BTC", "ETH"])
    assert item.mentioned_coins == ["BTC", "ETH"]

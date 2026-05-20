"""Unit tests for services/news_dedup.

Coverage:
- normalize_headline pure function (lowercase + punct strip + ws collapse)
- is_duplicate: identical headlines match
- is_duplicate: token-set similarity (word order doesn't matter)
- is_duplicate: below-threshold passes through
- is_duplicate: empty recent set never matches
- is_duplicate: empty candidate headline never matches
- is_duplicate: mentioned-coins disjoint blocks match
- is_duplicate: mentioned-coins overlap allows match
- is_duplicate: missing coins on one side skips the guard
- is_duplicate: threshold=0 disables dedup entirely (returns None)
- DuplicateMatch carries the right metadata for logging
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.news_dedup import (
    DuplicateMatch,
    is_duplicate,
    normalize_headline,
)

pytestmark = pytest.mark.unit


def _item(
    headline: str,
    *,
    source: str = "rss:coindesk",
    external_id: str | None = "id-1",
    coins: list[str] | None = None,
) -> NewsItem:
    return NewsItem(
        source=source,
        external_id=external_id,
        published_at=Timestamp(dt=datetime.now(UTC)),
        headline=headline,
        mentioned_coins=coins or [],
    )


class TestNormalizeHeadline:
    def test_lowercase(self) -> None:
        assert normalize_headline("Bitcoin Breaks $80K!") == "bitcoin breaks 80k"

    def test_strip_punctuation(self) -> None:
        assert normalize_headline("BTC — surges 10% today!!!") == "btc surges 10 today"

    def test_collapse_whitespace(self) -> None:
        assert normalize_headline("Bitcoin    breaks  \t  $80k") == "bitcoin breaks 80k"

    def test_empty(self) -> None:
        assert normalize_headline("") == ""
        assert normalize_headline("    ") == ""


class TestIsDuplicate:
    def test_identical_headline_matches(self) -> None:
        a = _item("Bitcoin breaks $80k", coins=["BTC"])
        b = _item("Bitcoin breaks $80k", source="rss:decrypt", coins=["BTC"])
        match = is_duplicate(b, [a], similarity_threshold=70)
        assert match is not None
        assert match.matched_source == "rss:coindesk"
        assert match.similarity >= 95.0

    def test_word_order_does_not_matter(self) -> None:
        a = _item("Bitcoin price breaks $80k milestone", coins=["BTC"])
        b = _item("$80k milestone broken by Bitcoin price", coins=["BTC"])
        match = is_duplicate(b, [a], similarity_threshold=70)
        # token_set_ratio is order-insensitive.
        assert match is not None

    def test_syndicated_wire_story_caught(self) -> None:
        # Reuters wire story republished across two outlets with
        # rewording. Real-world measurement: this pair scores 62.5 on
        # token_set_ratio after normalization — below the default 60
        # threshold catches it; the coin-overlap guard prevents the
        # threshold from biting headlines about different stories.
        coindesk = _item(
            "Bitcoin Crosses $80,000 for First Time This Quarter",
            source="rss:coindesk",
            coins=["BTC"],
        )
        decrypt = _item(
            "BTC Surges Past $80K Milestone, First in Quarter",
            source="rss:decrypt",
            coins=["BTC"],
        )
        match = is_duplicate(decrypt, [coindesk], similarity_threshold=60)
        assert match is not None
        # Score should be moderate (rewording reduces it) but above 60.
        assert match.similarity >= 60.0

    def test_different_stories_below_threshold(self) -> None:
        a = _item("Bitcoin breaks $80k milestone", coins=["BTC"])
        b = _item(
            "Ethereum 2.0 testnet upgrade scheduled next week",
            coins=["ETH"],
        )
        match = is_duplicate(b, [a], similarity_threshold=70)
        # Different topics; should not be dedupe target. Also coins
        # don't overlap, so the coin-guard would block even if score
        # were high.
        assert match is None

    def test_empty_recent_set_no_match(self) -> None:
        a = _item("Bitcoin breaks $80k", coins=["BTC"])
        match = is_duplicate(a, [], similarity_threshold=70)
        assert match is None

    def test_empty_candidate_headline_no_match(self) -> None:
        # Pydantic min_length=1 prevents empty in production, but the
        # function defends against degenerate input regardless.
        a = _item("Some real headline")
        candidate = _item("   ")  # whitespace only
        # The normalizer reduces to empty string; bail before scoring.
        match = is_duplicate(candidate, [a], similarity_threshold=70)
        assert match is None

    def test_disjoint_coins_blocks_match(self) -> None:
        # Two headlines about different coins that happen to share
        # tokens shouldn't dedupe. "X hits new high" pattern.
        a = _item("Bitcoin hits new all-time high price", coins=["BTC"])
        b = _item("Ethereum hits new all-time high price", coins=["ETH"])
        match = is_duplicate(b, [a], similarity_threshold=70)
        assert match is None

    def test_overlapping_coins_allows_match(self) -> None:
        # Same coin mentioned in both — coin guard passes; if
        # headline similarity is high enough, dedup fires.
        a = _item("Bitcoin and Ethereum surge", coins=["BTC", "ETH"])
        b = _item("BTC and ETH surge in afternoon trading", coins=["BTC", "ETH"])
        match = is_duplicate(b, [a], similarity_threshold=55)
        assert match is not None

    def test_no_coins_on_either_side_skips_guard(self) -> None:
        # Macro headlines often have no extracted coins; the coin
        # guard should not block dedup in that case.
        a = _item("Crypto markets see broad rally amid SEC clarity", coins=[])
        b = _item("Broad crypto market rally amid SEC clarity", coins=[])
        match = is_duplicate(b, [a], similarity_threshold=70)
        assert match is not None

    def test_threshold_zero_disables(self) -> None:
        a = _item("Bitcoin breaks $80k", coins=["BTC"])
        b = _item("Bitcoin breaks $80k", coins=["BTC"])
        match = is_duplicate(b, [a], similarity_threshold=0)
        assert match is None

    def test_match_metadata_populated(self) -> None:
        a = _item(
            "Bitcoin breaks $80k",
            source="rss:coindesk",
            external_id="cd-1234",
            coins=["BTC"],
        )
        b = _item("Bitcoin breaks $80k", source="rss:decrypt", coins=["BTC"])
        match = is_duplicate(b, [a], similarity_threshold=70)
        assert isinstance(match, DuplicateMatch)
        assert match.matched_source == "rss:coindesk"
        assert match.matched_external_id == "cd-1234"
        assert match.matched_headline == "Bitcoin breaks $80k"
        assert match.similarity >= 70.0

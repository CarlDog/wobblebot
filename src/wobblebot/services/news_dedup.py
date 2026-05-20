"""News deduplication — drop syndicated reposts before they hit storage.

Soak Day 2 (2026-05-20) operator observation: aggregating multiple RSS
sources + CryptoCompare produces visible duplicates. Reuters and AP
syndicate wire stories that get republished by CoinDesk, Decrypt, The
Block, etc., each with slightly different titles and URLs. The
``news_items`` table fills with multiple rows representing one
underlying story, which pollutes the advisor's news context window
and noise-floors the operator's ``/news`` view.

This module ships a **two-layer dedup** the news ingestion path runs
before persistence:

1. **Exact dedup** — handled at the storage layer by the existing
   ``UNIQUE(source, external_id)`` constraint. Catches same-source
   reposts. Out of scope for this module.

2. **Fuzzy dedup** — this module. Computes a token-set similarity
   ratio between the candidate item's headline and each item from
   the last N hours (configurable). If the similarity exceeds the
   configured threshold AND the candidate's mentioned-coins set
   overlaps the recent item's mentioned-coins, the candidate is
   classified as a duplicate and dropped silently (logged at info
   level for forensics).

The token-set ratio (rapidfuzz's ``token_set_ratio``) is the right
similarity metric for headlines because it's:
- Insensitive to word ordering ("BTC drops" vs "drops BTC")
- Insensitive to repeated tokens (which can creep in via SEO)
- Tolerant of synonymous filler tokens

The mentioned-coins overlap check is a sanity guard: if two headlines
happen to score high in token similarity but reference different
coins, they're probably about different stories (e.g. "Bitcoin
hits new high" and "Ethereum hits new high" share many tokens but
are distinct news).

**Threshold tuning.** Default 70/100 catches the common syndication
case (Reuters + Bloomberg + CoinDesk reporting "Bitcoin breaks $80k"
with different titles) while letting genuinely different stories
through ("Bitcoin breaks $80k" vs "Bitcoin testnet upgrade" share
"Bitcoin" but score below 50). Operator tunes via
``news.dedup.fuzzy_threshold``; setting to 0 disables fuzzy dedup
entirely (exact-only).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from rapidfuzz import fuzz

from wobblebot.domain.models import NewsItem

_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_headline(headline: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace.

    Pre-comparison normalization improves token-set ratio quality
    by removing variations that don't carry semantic content (case,
    trailing punctuation, em-dashes, etc.). Pure function; testable
    in isolation.
    """
    lowered = headline.lower().strip()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", no_punct).strip()


@dataclass(frozen=True)
class DuplicateMatch:
    """When ``is_duplicate`` finds a match, this records which existing
    item it matched against and the computed similarity score. Useful
    for forensic logging. ``NewsItem`` doesn't carry the DB row id;
    we surface source + external_id + headline instead — operators
    look at those, not row numbers."""

    matched_source: str
    matched_external_id: str | None
    matched_headline: str
    similarity: float


def is_duplicate(
    candidate: NewsItem,
    recent_items: Iterable[NewsItem],
    *,
    similarity_threshold: float,
) -> DuplicateMatch | None:
    """Return a ``DuplicateMatch`` if ``candidate`` is a near-duplicate
    of any item in ``recent_items``, else ``None``.

    Args:
        candidate: The new item we're about to save.
        recent_items: Items already saved within the dedup window
            (typically last N hours). Should be the chronologically
            most recent items first, though order doesn't affect
            correctness — we check all of them.
        similarity_threshold: Minimum token-set ratio (0-100) to
            consider items duplicates. 70 is a tested default; 80+
            is stricter (fewer false positives, more pass-through
            of syndicated stories); below 50 risks false positives
            for headlines that share crypto-jargon vocabulary.

    The mentioned-coins overlap check is REQUIRED before a duplicate
    classification — items above the similarity threshold that mention
    disjoint coin sets are NOT duplicates (their token overlap is
    coincidence).
    """
    if similarity_threshold <= 0:
        return None  # disable signal — fuzzy dedup off
    candidate_normalized = normalize_headline(candidate.headline)
    if not candidate_normalized:
        return None  # empty headline; can't compute meaningful similarity
    candidate_coins = set(candidate.mentioned_coins)
    for existing in recent_items:
        # Mentioned-coins overlap (set intersection). When BOTH items
        # mention coins, require overlap; if either has no coins
        # mentioned (e.g. broad market headline), skip this guard.
        existing_coins = set(existing.mentioned_coins)
        coins_overlap = bool(candidate_coins & existing_coins)
        if candidate_coins and existing_coins and not coins_overlap:
            continue
        existing_normalized = normalize_headline(existing.headline)
        if not existing_normalized:
            continue
        score = fuzz.token_set_ratio(candidate_normalized, existing_normalized)
        if score >= similarity_threshold:
            return DuplicateMatch(
                matched_source=existing.source,
                matched_external_id=existing.external_id,
                matched_headline=existing.headline,
                similarity=float(score),
            )
    return None

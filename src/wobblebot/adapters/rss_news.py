"""RssNewsAdapter — Stage 3.2.5 ``NewsPort`` implementation backed by an RSS/Atom feed.

One instance = one feed URL. The operator instantiates the adapter
per feed (CoinDesk, Decrypt, The Block, etc.) and the polling loop
fans out across all of them.

**Why feedparser, not hand-rolled XML.** RSS/Atom feeds vary
wildly: RSS 0.9 vs 1.0 vs 2.0, Atom 0.3 vs 1.0, malformed
namespaces, mixed encoding, missing fields. feedparser handles all
of this transparently and has been maintained for 15+ years. The
~100 lines we'd write to handle the cases that matter would either
miss cases or grow into a maintenance burden.

**Why httpx, not feedparser's own fetcher.** feedparser can fetch
URLs itself, but our test seam wants HTTP control. We fetch the
bytes with httpx (MockTransport in tests), then pass the bytes to
``feedparser.parse(bytes)`` for parsing. That keeps the adapter
testable without spinning up a fake HTTP server.

**Coin extraction.** ``mentioned_coins`` is populated by scanning
the headline + body for known crypto names + ticker symbols. The
whitelist is small (top-10ish coins by relevance) — false negatives
on niche alts are acceptable; false positives ("USA" matching
"USAcoin", "ICO" matching nothing) would be worse. Sources that
don't carry sentiment scores (most RSS feeds) get ``None``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx

from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NewsError
from wobblebot.ports.news import NewsPort

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_USER_AGENT = "wobblebot/0.1 (+rss-news)"

# Whitelist of common cryptocurrencies. Keep small — false positives
# (matching "USA" or "SEC" as ticker symbols) are worse than missing a
# niche alt. Ordered by approximate relevance for grid-trader workflows.
_COIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("BTC", re.compile(r"\b(BTC|Bitcoin)\b", re.IGNORECASE)),
    ("ETH", re.compile(r"\b(ETH|Ether(?:eum)?)\b", re.IGNORECASE)),
    ("SOL", re.compile(r"\b(SOL|Solana)\b", re.IGNORECASE)),
    ("DOGE", re.compile(r"\b(DOGE|Dogecoin)\b", re.IGNORECASE)),
    ("ADA", re.compile(r"\b(ADA|Cardano)\b", re.IGNORECASE)),
    ("XRP", re.compile(r"\b(XRP|Ripple)\b", re.IGNORECASE)),
    ("DOT", re.compile(r"\b(DOT|Polkadot)\b", re.IGNORECASE)),
    ("MATIC", re.compile(r"\b(MATIC|Polygon)\b", re.IGNORECASE)),
    ("AVAX", re.compile(r"\b(AVAX|Avalanche)\b", re.IGNORECASE)),
    ("LINK", re.compile(r"\b(LINK|Chainlink)\b", re.IGNORECASE)),
)


def _extract_mentioned_coins(*texts: str) -> list[str]:
    """Return the ordered set of whitelisted coins mentioned across ``texts``."""
    seen: dict[str, None] = {}
    for code, pattern in _COIN_PATTERNS:
        for text in texts:
            if text and pattern.search(text):
                seen[code] = None
                break
    return list(seen)


def _strptime_struct(t: struct_time | None) -> datetime | None:
    """Convert feedparser's struct_time (always UTC) to an aware ``datetime``."""
    if t is None:
        return None
    return datetime(*t[:6], tzinfo=UTC)


def _entry_published_at(entry: Any) -> datetime | None:
    """Pull the best-available publication timestamp from a feedparser entry."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        candidate = entry.get(attr)
        if candidate is not None:
            return _strptime_struct(candidate)
    return None


def _entry_external_id(entry: Any) -> str | None:
    """Pull a stable per-item ID for dedup. RSS calls it ``guid``, Atom ``id``."""
    for attr in ("id", "guid", "link"):
        value = entry.get(attr)
        if value:
            return str(value)
    return None


def _entry_body(entry: Any) -> str:
    """Best-effort body text from the entry. Returns empty string when none."""
    for attr in ("summary", "description", "subtitle"):
        value = entry.get(attr)
        if value:
            return str(value)
    return ""


class RssNewsAdapter(NewsPort):
    """One RSS/Atom feed, surfaced as a ``NewsPort``.

    Args:
        source_id: Short stable ID for this feed
            (``"rss:coindesk"``, ``"rss:decrypt"``, ...). Becomes
            ``NewsItem.source`` and pairs with the entry's GUID/id
            for dedup at the storage layer.
        feed_url: Absolute URL of the RSS/Atom feed.
        timeout_seconds: HTTP read timeout. 30s default; feeds with
            slow upstream renderers may need 60+.
        client: Optional pre-built ``httpx.AsyncClient`` (test seam).
            If ``None``, the adapter creates its own and ``aclose()``
            releases it.
        user_agent: Sent as the ``User-Agent`` header. Some feeds
            block the default ``python-requests`` style strings;
            identifying ourselves is polite.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        source_id: str,
        feed_url: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        if not source_id:
            raise ValueError("source_id is required")
        if not feed_url:
            raise ValueError("feed_url is required")
        self._source_id = source_id
        self._feed_url = feed_url
        self._user_agent = user_agent
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def source_id(self) -> str:
        return self._source_id

    async def aclose(self) -> None:
        """Release the owned httpx client, if any."""
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[NewsItem]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml",
        }
        try:
            response = await self._client.get(self._feed_url, headers=headers)
            response.raise_for_status()
            raw = response.content
        except httpx.HTTPError as exc:
            raise NewsError(f"RSS fetch failed for {self._feed_url}: {exc}") from exc

        parsed = feedparser.parse(raw)
        # feedparser sets bozo=1 when the feed is malformed. It still
        # produces entries in many cases, but we treat truly empty
        # parses as an upstream failure rather than legit silence.
        if not parsed.entries and getattr(parsed, "bozo", False):
            bozo_exc = getattr(parsed, "bozo_exception", None)
            raise NewsError(
                f"RSS parse failed for {self._feed_url}: {bozo_exc or 'malformed feed'}"
            )

        now_ts = Timestamp(dt=datetime.now(UTC))
        items: list[NewsItem] = []
        for entry in parsed.entries:
            published = _entry_published_at(entry)
            if published is None:
                # Feeds without per-entry timestamps are too lossy to
                # store usefully (no dedup, no ordering). Skip.
                continue
            headline = (entry.get("title") or "").strip()
            if not headline:
                # NewsItem requires non-empty headline; skip rather
                # than fail the whole batch.
                continue
            body = _entry_body(entry).strip()
            items.append(
                NewsItem(
                    source=self._source_id,
                    external_id=_entry_external_id(entry),
                    published_at=Timestamp(dt=published),
                    headline=headline,
                    body=body,
                    sentiment_score=None,
                    mentioned_coins=_extract_mentioned_coins(headline, body),
                    fetched_at=now_ts,
                )
            )
        # Storage stores DESC; our port contract says return ASC (oldest first).
        items.sort(key=lambda it: it.published_at.dt)
        return items

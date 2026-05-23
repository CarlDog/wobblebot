"""CryptoCompareAdapter — Stage 3.2.5 ``NewsPort`` implementation backed by CryptoCompare News.

Polls the public ``/data/v2/news/`` endpoint. Free tier requires an
API key (since the 2025 CoinDesk Data migration); polling at 15-30
minute intervals fits well inside the rate budget.

**Sentiment.** CryptoCompare does not expose a clean per-article
sentiment classification. Their ``upvotes`` / ``downvotes`` fields
reflect community engagement, not article tone — a high-vote article
can be neutral or negative. We set ``sentiment_score=None``; the
Stage 3.4a news expert is responsible for deriving tone from the
headline + body text via the LLM.

**Mentioned coins.** Pulled from the response's ``categories`` field
(pipe-separated string like ``"BTC|ETH|Trading"``). We filter to
entries that look like ticker codes (2-5 uppercase alphanumeric
chars), which is structurally cleaner than the RSS adapter's regex
scan over headline text.

**Auth.** The API key is sent in the ``authorization`` header as
``Apikey <key>``. Don't pass it as a query param — that puts it in
upstream logs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import NewsError
from wobblebot.ports.news import NewsPort

_DEFAULT_BASE_URL = "https://min-api.cryptocompare.com"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_SOURCE_ID = "cryptocompare"

# Currency-code-shaped tokens in the categories field. Filters out
# topic tags like "Trading", "Mining", "Regulation" that aren't coins.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9]{2,5}$")


def _extract_coins_from_categories(categories: str) -> list[str]:
    """Split CryptoCompare's pipe-delimited categories and keep only ticker-shaped tokens."""
    if not categories:
        return []
    tokens = [tok.strip() for tok in categories.split("|") if tok.strip()]
    return [tok for tok in tokens if _TICKER_PATTERN.fullmatch(tok)]


class CryptoCompareAdapter(NewsPort):
    """Aggregated crypto news via CryptoCompare's ``/data/v2/news/`` endpoint.

    Args:
        api_key: CryptoCompare API key (scoped "Poll Live and
            Historical Data" minimum). Required even on the free tier
            since the 2025 CoinDesk Data migration.
        lang: Language filter (default ``"EN"``). CryptoCompare also
            supports ``"ES"``, ``"PT"``, ``"FR"``, etc.
        categories: Optional category filter (pipe-separated). When
            ``None``, the endpoint returns all categories.
        base_url: API base URL. Defaults to the public host.
        timeout_seconds: HTTP read timeout.
        client: Optional pre-built ``httpx.AsyncClient`` (test seam).
            If ``None``, the adapter owns one and ``aclose()`` releases it.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        api_key: str,
        lang: str = "EN",
        categories: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for CryptoCompareAdapter")
        self._api_key = api_key
        self._lang = lang
        self._categories = categories
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    async def aclose(self) -> None:
        """Release the owned httpx client, if any."""
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[NewsItem]:
        params: dict[str, str] = {"lang": self._lang}
        if self._categories is not None:
            params["categories"] = self._categories
        headers = {
            "authorization": f"Apikey {self._api_key}",
            "Accept": "application/json",
        }
        try:
            response = await self._client.get(
                f"{self._base_url}/data/v2/news/", params=params, headers=headers
            )
            response.raise_for_status()
            envelope: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise NewsError(f"CryptoCompare fetch failed: {exc}") from exc

        if envelope.get("Type") != 100:
            raise NewsError(
                f"CryptoCompare returned unexpected envelope type "
                f"{envelope.get('Type')!r}: {envelope.get('Message', '<no message>')}"
            )

        raw_items = envelope.get("Data")
        if not isinstance(raw_items, list):
            raise NewsError(
                f"CryptoCompare envelope missing 'Data' list; got {type(raw_items).__name__}"
            )

        now_ts = Timestamp(dt=datetime.now(UTC))
        items: list[NewsItem] = []
        for raw in raw_items:
            item = _row_to_news_item(raw, now_ts)
            if item is not None:
                items.append(item)
        # Sort ASC by published_at — port contract is oldest-first.
        items.sort(key=lambda it: it.published_at.dt)
        return items


def _row_to_news_item(raw: dict[str, Any], fetched_at: Timestamp) -> NewsItem | None:
    """Map one CryptoCompare row into a NewsItem. Returns None for unusable rows."""
    title = (raw.get("title") or "").strip()
    if not title:
        # NewsItem requires non-empty headline.
        return None
    published_on = raw.get("published_on")
    if not isinstance(published_on, (int, float)):
        # Without a timestamp, can't dedup or order — skip.
        return None
    try:
        published = datetime.fromtimestamp(float(published_on), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None

    external_id = raw.get("id")
    body = (raw.get("body") or "").strip()
    coins = _extract_coins_from_categories(raw.get("categories") or "")
    # CryptoCompare aggregates from many upstream publishers (CoinDesk,
    # Bloomberg crypto, etc.). The source_info.name field carries the
    # original publisher; surface it so the web UI can show "CoinDesk
    # (via cryptocompare)" rather than just "cryptocompare".
    source_info = raw.get("source_info") or {}
    publisher = source_info.get("name") if isinstance(source_info, dict) else None
    url = raw.get("url")

    return NewsItem(
        source=_SOURCE_ID,
        external_id=str(external_id) if external_id is not None else None,
        published_at=Timestamp(dt=published),
        headline=title,
        body=body,
        sentiment_score=None,
        mentioned_coins=coins,
        fetched_at=fetched_at,
        publisher=publisher if isinstance(publisher, str) and publisher else None,
        url=str(url) if isinstance(url, str) and url else None,
    )

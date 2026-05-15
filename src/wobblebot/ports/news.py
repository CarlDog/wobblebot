"""NewsPort — Abstract interface for fetching news items.

Per ADR-007, news ingestion is a separate, parallel abstraction
alongside ``AdvisorPort`` — the advisor consumes news, the news
source is independent of the advisor implementation. Each adapter
represents **one source** (a single RSS feed, one aggregator API,
etc.); the polling loop fans out across multiple ``NewsPort``
instances.

Adapters MUST raise ``NewsError`` on transport / protocol / parse
failures. The polling loop logs and continues with the remaining
sources — one bad feed cannot stop the rest. News-derived
recommendations never auto-apply (also per ADR-007); the
``NewsPort`` contract has no notion of execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from wobblebot.domain.models import NewsItem


class NewsPort(ABC):
    """Abstract interface for one news source.

    Implementations:
    - ``RssNewsAdapter`` — one instance per RSS feed URL.
    - ``CryptoCompareAdapter`` — aggregated crypto news.
    - Operator-supplied adapters can plug in via the same interface;
      paid sources (CryptoPanic, Whale-alert) live here if the
      operator chooses to subscribe.

    Error convention:
    - Protocol / transport / parse failure raises ``NewsError``.
    - Empty result (the feed has no new items since last poll) is
      NOT an error — return ``[]``.
    """

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Short stable identifier for this source.

        Examples: ``"rss:coindesk"``, ``"cryptocompare"``. Used as
        the ``NewsItem.source`` field; together with
        ``NewsItem.external_id`` forms the storage dedup key.
        """

    @abstractmethod
    async def fetch(self) -> list[NewsItem]:
        """Pull the latest items from the source.

        Returns:
            Items ordered by ``published_at`` ascending (oldest
            first). Empty list if nothing is available — common at
            most poll intervals.

        Raises:
            NewsError: If the source is unreachable or returns
                invalid data.
        """

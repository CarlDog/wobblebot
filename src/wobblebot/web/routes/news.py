"""News view — reads news.db's news_items (Stage 7.4.A).

Read-only listing of recent crypto-news headlines surfaced by
``cli/news``. Per-source filter via ``?source=...`` query param;
optional per-coin filter via ``?coin=...`` (matches against
``NewsItem.mentioned_coins`` server-side).

Graceful-degrades when ``news_storage`` is unwired.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, Response

from wobblebot.domain.models import NewsItem
from wobblebot.domain.users import User
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort
from wobblebot.web.auth import require_user
from wobblebot.web.dependencies import get_news_storage, get_templates

router = APIRouter(tags=["news"])

# Display the most-recent N items; pull a wider slice for the
# total count + sources dropdown. Soak-period news volume varies
# wildly with the fuzzy-dedup threshold; 1000 row cap is roomy.
_NEWS_DISPLAY_LIMIT = 30
_NEWS_QUERY_LIMIT = 1000


@dataclass(frozen=True)
class NewsSnapshot:
    wired: bool
    items: tuple[NewsItem, ...]
    sources: tuple[str, ...]
    source_filter: str | None
    coin_filter: str | None
    total: int = 0
    error: str | None = None


async def _load_snapshot(
    news_storage: StoragePort | None,
    *,
    source_filter: str | None,
    coin_filter: str | None,
) -> NewsSnapshot:
    if news_storage is None:
        return NewsSnapshot(
            wired=False,
            items=(),
            sources=(),
            source_filter=source_filter,
            coin_filter=coin_filter,
        )
    try:
        rows = await news_storage.get_news_items(
            source=source_filter,
            limit=_NEWS_QUERY_LIMIT,
        )
    except StorageError as exc:
        return NewsSnapshot(
            wired=True,
            items=(),
            sources=(),
            source_filter=source_filter,
            coin_filter=coin_filter,
            error=f"failed to query news_items: {exc}",
        )
    # Server-side coin filter — case-insensitive substring match
    # against mentioned_coins entries.
    if coin_filter:
        needle = coin_filter.upper()
        rows = [r for r in rows if any(needle in c.upper() for c in r.mentioned_coins)]
    # Total after filtering = the "real" count of matching items.
    total = len(rows)
    # Distinct sources for the filter dropdown — pull from a wider
    # unfiltered slice so the dropdown is stable across filtered views.
    try:
        all_rows = await news_storage.get_news_items(limit=_NEWS_QUERY_LIMIT)
        sources = tuple(sorted({r.source for r in all_rows}))
    except StorageError:
        sources = ()
    return NewsSnapshot(
        wired=True,
        items=tuple(rows[:_NEWS_DISPLAY_LIMIT]),
        sources=sources,
        source_filter=source_filter,
        coin_filter=coin_filter,
        total=total,
    )


# pylint: disable=too-many-arguments,too-many-positional-arguments
@router.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    source: str | None = Query(default=None, min_length=0, max_length=64),
    coin: str | None = Query(default=None, min_length=0, max_length=16),
    user: User = Depends(require_user),
    news_storage: StoragePort | None = Depends(get_news_storage),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    snapshot = await _load_snapshot(
        news_storage,
        source_filter=source or None,
        coin_filter=coin or None,
    )
    return templates.TemplateResponse(
        request,
        "news.html",
        {"snapshot": snapshot, "username": user.username},
    )


__all__ = ("router", "NewsSnapshot")

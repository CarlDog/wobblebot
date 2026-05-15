"""News CLI — long-running news ingestion daemon (Stage 3.2.5).

Run as a module::

    python -m wobblebot.cli.news
    python -m wobblebot.cli.news --profile aggressive
    python -m wobblebot.cli.news --poll-interval-minutes 15

**Read-only against external sources; write-only against local
storage.** Polls every enabled ``NewsPort`` (RSS feeds + optional
CryptoCompare) on the configured interval and persists each item
to the local ``news_items`` table. Dedup is enforced at the storage
layer via ``UNIQUE(source, external_id)`` — re-fetching the same
article across polls is a no-op.

**Fault isolation.** One bad source (DNS failure, 500, malformed
feed) cannot stop the others. Per-source errors are logged and the
loop continues with the remaining sources both this tick and next.

**Config layering** (per ADR-009):
1. Base config — ``config/settings.yml``.
2. Profile overrides — ``--profile name``.
3. CLI flag overrides — explicit flags below.

CryptoCompare requires ``$CRYPTOCOMPARE_API_KEY`` in the
environment when ``news.cryptocompare.enabled: true``. Missing the
key with the flag on is an error (exit 2). RSS feeds need no auth.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import timedelta
from typing import Any

from wobblebot.adapters.cryptocompare_news import CryptoCompareAdapter
from wobblebot.adapters.rss_news import RssNewsAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.cli import NewsConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import NewsError, StorageError
from wobblebot.ports.news import NewsPort

_LOGGER = logging.getLogger("wobblebot.cli.news")


def _build_sources(news: NewsConfig) -> list[NewsPort]:
    """Instantiate every enabled NewsPort from the config."""
    sources: list[NewsPort] = []
    for feed in news.rss_feeds:
        if not feed.enabled:
            continue
        sources.append(RssNewsAdapter(source_id=feed.source_id, feed_url=feed.url))
    if news.cryptocompare.enabled:
        api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "news.cryptocompare.enabled=true but CRYPTOCOMPARE_API_KEY is not set"
            )
        sources.append(
            CryptoCompareAdapter(
                api_key=api_key,
                lang=news.cryptocompare.lang,
                categories=news.cryptocompare.categories,
            )
        )
    return sources


async def _poll_source(source: NewsPort, storage: SQLiteStorageAdapter) -> tuple[int, int]:
    """Fetch + persist one source. Returns (fetched, saved). Logs failures."""
    try:
        items = await source.fetch()
    except NewsError as exc:
        _LOGGER.error(
            "news fetch failed",
            extra={
                "source_id": source.source_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return (0, 0)

    saved = 0
    for item in items:
        try:
            await storage.save_news_item(item)
            saved += 1
        except StorageError as exc:
            _LOGGER.error(
                "news item save failed",
                extra={
                    "source_id": source.source_id,
                    "headline": item.headline[:80],
                    "error": str(exc),
                },
            )
    _LOGGER.info(
        "news poll complete",
        extra={
            "source_id": source.source_id,
            "fetched": len(items),
            "saved": saved,
        },
    )
    return (len(items), saved)


async def _run_loop(
    sources: list[NewsPort],
    storage: SQLiteStorageAdapter,
    news: NewsConfig,
    interval: timedelta,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    total_fetched = 0
    total_saved = 0
    interval_seconds = interval.total_seconds()
    _LOGGER.info(
        "news session start",
        extra={
            "sources": [s.source_id for s in sources],
            "interval_seconds": interval_seconds,
            "db_path": news.db,
        },
    )
    try:
        while not stop_event.is_set():
            for source in sources:
                if stop_event.is_set():
                    break
                fetched, saved = await _poll_source(source, storage)
                total_fetched += fetched
                total_saved += saved

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        _LOGGER.info(
            "news session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "total_fetched": total_fetched,
                "total_saved": total_saved,
            },
        )
    return 0


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


async def _main_async(config: WobbleBotConfig) -> int:
    if config.news is None:
        _LOGGER.error("settings.yml is missing the `news:` section")
        return 2

    try:
        interval = config.schedules.get("news")
    except KeyError as exc:
        _LOGGER.error("missing schedule", extra={"error": str(exc)})
        return 2

    try:
        sources = _build_sources(config.news)
    except RuntimeError as exc:
        _LOGGER.error("news setup failed", extra={"error": str(exc)})
        return 2

    if not sources:
        _LOGGER.error("no enabled news sources; nothing to poll")
        return 2

    storage = SQLiteStorageAdapter(config.news.db)
    await storage.connect()

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(sources, storage, config.news, interval, stop_event)
    finally:
        for source in sources:
            aclose = getattr(source, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except (NewsError, OSError) as exc:
                    _LOGGER.warning(
                        "source close failed",
                        extra={"source_id": source.source_id, "error": str(exc)},
                    )
        await storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "news",
        {
            "db": ("db", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument("--db", default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = config.news.log_format if config.news else "plain"
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

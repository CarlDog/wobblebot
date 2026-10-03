"""Failed sources slow down without delaying healthy feeds or heartbeats."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from wobblebot.cli import news as daemon
from wobblebot.config.cli import NewsConfig
from wobblebot.ports.exceptions import NewsError, StorageError
from wobblebot.services.news_backoff import SourceBackoff

pytestmark = pytest.mark.unit


def test_backoff_is_bounded_and_empty_success_resets():
    state = SourceBackoff(60)
    assert state.failed(0) == 60
    assert state.failed(60) == 120
    assert not state.due(179)
    assert state.due(180)
    for _ in range(100):
        assert state.failed(180) <= 21600
    state.succeeded()
    assert state.due(180) and state.failed(180) == 60
    assert SourceBackoff(86400).failed(0) == 86400


@pytest.mark.asyncio
async def test_failed_feed_does_not_delay_healthy_feed_or_heartbeat(monkeypatch):
    clock = [0.0]
    healthy = AsyncMock(source_id="rss:healthy")
    healthy.fetch.return_value = []
    failed = AsyncMock(source_id="rss:failed")
    failed.fetch.side_effect = [NewsError("unavailable"), NewsError("unavailable"), [], []]
    storage = AsyncMock()
    storage.get_news_items.return_value = []
    heartbeat = AsyncMock()
    monkeypatch.setattr(daemon, "emit_heartbeat", heartbeat)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])

    async def cycles(callback, **_kwargs):
        for now in (0, 60, 120, 180, 240):
            clock[0] = now
            await callback()

    monkeypatch.setattr(daemon, "run_poll_loop", cycles)
    assert (
        await daemon._run_loop(
            [failed, healthy],
            storage,
            NewsConfig(rss_feeds=[]),
            timedelta(seconds=60),
            asyncio.Event(),
        )
        == 0
    )
    assert healthy.fetch.await_count == 5
    assert failed.fetch.await_count == 4  # skips only 120, recovers at 180
    assert heartbeat.await_count == 5
    storage.save_news_item.assert_not_called()


@pytest.mark.asyncio
async def test_storage_failure_is_not_misclassified_as_source_failure():
    source = AsyncMock(source_id="rss:healthy")
    source.fetch.return_value = []
    storage = AsyncMock()
    storage.get_news_items.side_effect = StorageError("database unavailable")
    state = SourceBackoff(60, failures=3, next_attempt=100)
    await daemon._poll_source(source, storage, NewsConfig().dedup, backoff=state)
    assert state.failures == 0

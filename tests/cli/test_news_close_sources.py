"""Tests for cli/news._close_news_sources.

Regression coverage for the 2026-05-25 shutdown audit: each NewsPort
adapter's ``aclose()`` was awaited without a per-source timeout, so
a single httpx-backed source with a stuck pool connection could
hold up the whole shutdown sequence. The helper caps each close at
2s and continues to the next source on overrun.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from wobblebot.cli.news import _close_news_sources
from wobblebot.ports.exceptions import NewsError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeNewsSource:
    """Minimal NewsPort surface: only the methods the helper uses."""

    def __init__(
        self,
        source_id: str,
        *,
        close_seconds: float = 0.0,
        close_raises: BaseException | None = None,
    ) -> None:
        self.source_id = source_id
        self._close_seconds = close_seconds
        self._close_raises = close_raises
        self.close_called = False
        self.close_completed = False

    async def aclose(self) -> None:
        self.close_called = True
        if self._close_seconds > 0:
            await asyncio.sleep(self._close_seconds)
        if self._close_raises is not None:
            raise self._close_raises
        self.close_completed = True


class TestCloseNewsSourcesHappyPath:
    async def test_closes_each_source_in_order(self) -> None:
        sources = [
            _FakeNewsSource("rss:coindesk"),
            _FakeNewsSource("cryptocompare"),
            _FakeNewsSource("rss:decrypt"),
        ]
        await _close_news_sources(sources, per_source_timeout_seconds=1.0)  # type: ignore[arg-type]
        assert all(s.close_completed for s in sources)

    async def test_skips_sources_without_aclose(self) -> None:
        """An adapter that doesn't expose aclose (e.g. a stub backed by
        local files) is skipped silently — the helper is best-effort."""

        class _NoCloseSource:
            source_id = "stub"

        sources = [_NoCloseSource(), _FakeNewsSource("rss:real")]
        await _close_news_sources(sources, per_source_timeout_seconds=1.0)  # type: ignore[arg-type]
        assert sources[1].close_completed  # type: ignore[attr-defined]


class TestCloseNewsSourcesTimeout:
    async def test_slow_source_does_not_block_subsequent(self) -> None:
        """The whole point: a wedged source can't hold up the others."""
        slow = _FakeNewsSource("slow-feed", close_seconds=5.0)
        fast = _FakeNewsSource("fast-feed", close_seconds=0.0)
        await _close_news_sources([slow, fast], per_source_timeout_seconds=0.05)  # type: ignore[arg-type]
        assert fast.close_completed
        # slow's close was started but timed out before it could complete
        assert slow.close_called
        assert not slow.close_completed

    async def test_timeout_warning_includes_source_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Operators reading the log file need to know WHICH source
        timed out, not just that some source did."""
        slow = _FakeNewsSource("rss:slow-news.example.com", close_seconds=5.0)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.news"):
            await _close_news_sources([slow], per_source_timeout_seconds=0.05)  # type: ignore[arg-type]
        records = [r for r in caplog.records if "exceeded" in r.getMessage()]
        assert records
        assert getattr(records[0], "source_id", None) == "rss:slow-news.example.com"


class TestCloseNewsSourcesExceptions:
    async def test_news_error_during_close_is_logged_not_raised(self) -> None:
        """NewsError from aclose (transient adapter error during teardown)
        is logged and skipped — same as the legacy behavior pre-timeout."""
        bad = _FakeNewsSource("rss:bad-feed", close_raises=NewsError("transient teardown"))
        good = _FakeNewsSource("rss:good-feed")
        await _close_news_sources([bad, good], per_source_timeout_seconds=1.0)  # type: ignore[arg-type]
        assert good.close_completed

    async def test_oserror_during_close_is_logged_not_raised(self) -> None:
        """OSError from aclose (socket-level teardown failure) is
        logged + skipped, same as NewsError."""
        bad = _FakeNewsSource("rss:bad-feed", close_raises=OSError("conn reset"))
        good = _FakeNewsSource("rss:good-feed")
        await _close_news_sources([bad, good], per_source_timeout_seconds=1.0)  # type: ignore[arg-type]
        assert good.close_completed

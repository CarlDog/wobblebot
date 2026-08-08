"""Tests for cli/observe backfill log-line rendering.

Regression coverage for the 2026-05-25 manual-backfill smoke test
which surfaced that ``backfill complete for symbol`` was emitted
with stats tucked into ``extra={...}`` only -- invisible in the
default plain log format. Mirrors the
test_common_heartbeat.py contract (inline-into-message + preserve
extras for json consumers).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from wobblebot.cli.observe_backfill import (
    _log_backfill_result,
    _make_progress_logger,
    _warn_if_horizon_truncated,
)
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.backfill import BackfillResult

pytestmark = pytest.mark.unit


_BTC = Symbol(base="BTC", quote="USD")
_SINCE = datetime(2026, 5, 25, 16, 0, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 5, 25, 22, 0, 0, tzinfo=UTC)
_LAST = datetime(2026, 5, 25, 18, 30, 0, tzinfo=UTC)


def _make_result(*, error: str | None = None, **overrides: object) -> BackfillResult:
    base: dict[str, object] = {
        "symbol": _BTC,
        "interval_minutes": 1,
        "requested_since": _SINCE,
        "requested_until": _UNTIL,
        "bars_fetched": 362,
        "bars_inserted": 362,
        "snapshots_inserted": 362,
        "requests_made": 1,
        "elapsed_seconds": 1.27,
        "last_opened_at": _LAST,
        "error": error,
    }
    base.update(overrides)
    return BackfillResult(**base)  # type: ignore[arg-type]


class TestSuccessLogRendering:
    def test_success_message_includes_bars_inserted(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(_BTC, _make_result())
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "362" in rendered  # bars inserted count
        assert "BTC/USD" in rendered

    def test_success_message_includes_elapsed_seconds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(_BTC, _make_result(elapsed_seconds=12.34))
        assert any("12.3" in r.getMessage() for r in caplog.records)

    def test_success_message_includes_requests_made(self, caplog: pytest.LogCaptureFixture) -> None:
        """The Kraken-request count is the operator-visible API-burn
        signal; must be in the rendered line."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(_BTC, _make_result(requests_made=14))
        assert any("14 Kraken req" in r.getMessage() for r in caplog.records)

    def test_success_extras_still_populated(self, caplog: pytest.LogCaptureFixture) -> None:
        """Inline-into-message must NOT drop the structured extras --
        json-format operators still get the dict for aggregation."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(_BTC, _make_result())
        rec = next(r for r in caplog.records if "complete" in r.getMessage())
        assert getattr(rec, "bars_inserted", None) == 362
        assert getattr(rec, "snapshots_inserted", None) == 362


class TestProgressLogger:
    """P2 slice 1, item 3 — the per-chunk progress line every Nth request."""

    @pytest.mark.asyncio
    async def test_logs_on_every_tenth_request(self, caplog: pytest.LogCaptureFixture) -> None:
        callback = _make_progress_logger(_BTC)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            await callback(_make_result(requests_made=10, bars_fetched=7200))
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "7200 bars so far" in rendered
        assert "BTC/USD" in rendered
        assert _LAST.isoformat() in rendered  # the cursor

    @pytest.mark.asyncio
    async def test_silent_between_multiples(self, caplog: pytest.LogCaptureFixture) -> None:
        callback = _make_progress_logger(_BTC)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            for n in (1, 3, 7, 9, 11, 19):
                await callback(_make_result(requests_made=n))
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_no_cursor_renders_na(self, caplog: pytest.LogCaptureFixture) -> None:
        callback = _make_progress_logger(_BTC)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe_backfill"):
            await callback(_make_result(requests_made=10, last_opened_at=None))
        assert any("n/a" in r.getMessage() for r in caplog.records)


class TestErrorLogRendering:
    def test_error_message_includes_resume_cursor(self, caplog: pytest.LogCaptureFixture) -> None:
        """The whole point of the error path: tell the operator what
        --since value to use to resume. Must be in the rendered line,
        not just extras."""
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(
                _BTC,
                _make_result(error="ExchangeError: Kraken 500"),
            )
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "resume with --since" in rendered
        # The last_opened_at iso string is the resume hint.
        assert _LAST.isoformat() in rendered

    def test_error_message_includes_partial_progress(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An operator looking at the error needs the partial-progress
        count to know if 0 bars or 200 bars landed before failure."""
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(
                _BTC,
                _make_result(error="ExchangeError: foo", bars_inserted=200),
            )
        assert any("200" in r.getMessage() for r in caplog.records)

    def test_error_message_includes_error_text(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(
                _BTC,
                _make_result(error="StorageError: simulated disk full"),
            )
        assert any("simulated disk full" in r.getMessage() for r in caplog.records)

    def test_error_message_when_no_resume_cursor(self, caplog: pytest.LogCaptureFixture) -> None:
        """If the backfill failed before any successful page, there's
        no resume cursor. The message still renders cleanly without
        crashing on the None."""
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(
                _BTC,
                _make_result(
                    error="ExchangeError: auth failed",
                    last_opened_at=None,
                    bars_inserted=0,
                ),
            )
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "none" in rendered.lower()


class TestHorizonTruncationWarn:
    """P2 slice 1, item 7 — WARN when Kraken's retained history falls
    materially short of the requested window."""

    def _wide_result(self, *, bars_fetched: int) -> BackfillResult:
        # 30-day window at 1m => ~43,200 expected bars.
        return _make_result(
            requested_since=_UNTIL - timedelta(days=30),
            bars_fetched=bars_fetched,
        )

    def test_materially_short_result_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe_backfill"):
            _warn_if_horizon_truncated(self._wide_result(bars_fetched=720))
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "720 bars" in rendered
        assert "retained history" in rendered

    def test_full_result_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe_backfill"):
            _warn_if_horizon_truncated(self._wide_result(bars_fetched=43_000))
        assert not caplog.records

    def test_small_window_is_silent_even_when_short(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 6h window implies ~360 bars at 1m but only 90 at 4h — under
        the 100-expected floor, boundary noise dominates; never warn."""
        short = _make_result(interval_minutes=240, bars_fetched=10)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe_backfill"):
            _warn_if_horizon_truncated(short)
        assert not caplog.records

    def test_success_log_path_invokes_the_check(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe_backfill"):
            _log_backfill_result(_BTC, self._wide_result(bars_fetched=720))
        assert any("retained history" in r.getMessage() for r in caplog.records)

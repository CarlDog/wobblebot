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

from wobblebot.cli.observe import _log_backfill_result
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
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe"):
            _log_backfill_result(_BTC, _make_result())
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "362" in rendered  # bars inserted count
        assert "BTC/USD" in rendered

    def test_success_message_includes_elapsed_seconds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe"):
            _log_backfill_result(_BTC, _make_result(elapsed_seconds=12.34))
        assert any("12.3" in r.getMessage() for r in caplog.records)

    def test_success_message_includes_requests_made(self, caplog: pytest.LogCaptureFixture) -> None:
        """The Kraken-request count is the operator-visible API-burn
        signal; must be in the rendered line."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe"):
            _log_backfill_result(_BTC, _make_result(requests_made=14))
        assert any("14 Kraken req" in r.getMessage() for r in caplog.records)

    def test_success_extras_still_populated(self, caplog: pytest.LogCaptureFixture) -> None:
        """Inline-into-message must NOT drop the structured extras --
        json-format operators still get the dict for aggregation."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe"):
            _log_backfill_result(_BTC, _make_result())
        rec = next(r for r in caplog.records if "complete" in r.getMessage())
        assert getattr(rec, "bars_inserted", None) == 362
        assert getattr(rec, "snapshots_inserted", None) == 362


class TestErrorLogRendering:
    def test_error_message_includes_resume_cursor(self, caplog: pytest.LogCaptureFixture) -> None:
        """The whole point of the error path: tell the operator what
        --since value to use to resume. Must be in the rendered line,
        not just extras."""
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe"):
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
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe"):
            _log_backfill_result(
                _BTC,
                _make_result(error="ExchangeError: foo", bars_inserted=200),
            )
        assert any("200" in r.getMessage() for r in caplog.records)

    def test_error_message_includes_error_text(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe"):
            _log_backfill_result(
                _BTC,
                _make_result(error="StorageError: simulated disk full"),
            )
        assert any("simulated disk full" in r.getMessage() for r in caplog.records)

    def test_error_message_when_no_resume_cursor(self, caplog: pytest.LogCaptureFixture) -> None:
        """If the backfill failed before any successful page, there's
        no resume cursor. The message still renders cleanly without
        crashing on the None."""
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.observe"):
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

"""Tests for cli/_common.emit_heartbeat — the best-effort heartbeat helper.

Regression coverage for the 2026-05-25 diagnostic-gap incident: a
cli/harvest heartbeat write failed silently because the original
warning emitted the error class + message into ``extra={...}`` only,
which the plain log formatter doesn't render. Today's fix inlines
the daemon name + error class + message into the log line itself so
plain-format operators see actionable detail without switching to
json format.
"""

from __future__ import annotations

import logging

import pytest

from wobblebot.cli._common import emit_heartbeat
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _StubStorage:
    """Minimal StoragePort surface: only the method emit_heartbeat calls."""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.upsert_calls: list[tuple[str, object]] = []

    async def upsert_daemon_heartbeat(self, name: str, beat_at: object) -> None:
        self.upsert_calls.append((name, beat_at))
        if self._raises is not None:
            raise self._raises


class TestEmitHeartbeatHappyPath:
    async def test_writes_when_storage_provided(self) -> None:
        storage = _StubStorage()
        await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]
        assert len(storage.upsert_calls) == 1
        name, _beat_at = storage.upsert_calls[0]
        assert name == "cli/harvest"

    async def test_noop_when_storage_none(self) -> None:
        """``operator_storage=None`` short-circuits silently — covers the
        operator-without-operator_db config path."""
        await emit_heartbeat(None, "cli/harvest")


class TestEmitHeartbeatFailureVisibility:
    async def test_warning_inlines_daemon_name(self, caplog: pytest.LogCaptureFixture) -> None:
        """The daemon name must appear in the rendered message — not
        just in extra={...}, which plain format swallows."""
        storage = _StubStorage(raises=StorageError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.heartbeat"):
            await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]
        assert any("cli/harvest" in r.getMessage() for r in caplog.records)

    async def test_warning_inlines_error_class(self, caplog: pytest.LogCaptureFixture) -> None:
        """The exception class name must appear in the rendered message."""
        storage = _StubStorage(raises=StorageError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.heartbeat"):
            await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]
        assert any("StorageError" in r.getMessage() for r in caplog.records)

    async def test_warning_inlines_error_message(self, caplog: pytest.LogCaptureFixture) -> None:
        """The exception's message must appear in the rendered message."""
        storage = _StubStorage(raises=StorageError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.heartbeat"):
            await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]
        assert any("database is locked" in r.getMessage() for r in caplog.records)

    async def test_warning_extras_still_populated_for_json(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Inline-into-message doesn't drop the structured extras —
        json-format operators still get the dict for log aggregation."""
        storage = _StubStorage(raises=StorageError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.heartbeat"):
            await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]
        record = next(r for r in caplog.records if "cli/harvest" in r.getMessage())
        assert getattr(record, "daemon", None) == "cli/harvest"
        assert getattr(record, "error", None) == "database is locked"
        assert getattr(record, "error_type", None) == "StorageError"

    async def test_failure_does_not_raise(self) -> None:
        """The whole point of the helper: a StorageError must NEVER kill
        the calling daemon's tick loop. Today's incident proved the
        catch works; this test pins the contract."""
        storage = _StubStorage(raises=StorageError("any error"))
        # No pytest.raises — the call must return normally.
        await emit_heartbeat(storage, "cli/harvest")  # type: ignore[arg-type]

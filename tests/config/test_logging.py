"""Tests for the logging configuration."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest

from wobblebot.config.logging import configure_logging

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_wobblebot_logger():
    """Ensure each test starts from a clean wobblebot logger.

    configure_logging is idempotent for *its* own handler, but tests
    can still leak handlers added by other code paths. Snapshot and
    restore.

    Stage 8.2.D: any TimedRotatingFileHandler the test installed
    keeps an open file handle that needs explicit close before
    restoration — otherwise pytest's unraisable-exception hook
    catches the leak on session teardown.
    """
    logger = logging.getLogger("wobblebot")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    yield
    # Close any handlers the test added so file descriptors don't leak.
    for handler in logger.handlers:
        if handler not in original_handlers:
            try:
                handler.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
    logger.handlers = original_handlers
    logger.level = original_level
    logger.propagate = original_propagate


class TestPlainFormat:
    def test_plain_format_emits_human_readable_line(self):
        buf = io.StringIO()
        configure_logging(level="INFO", log_format="plain", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info("starting up")

        output = buf.getvalue().strip()
        # Format: "<asctime> [INFO] wobblebot.test: starting up"
        assert re.match(r".*\[INFO\] wobblebot\.test: starting up$", output)

    def test_plain_format_respects_level(self):
        buf = io.StringIO()
        configure_logging(level="WARNING", log_format="plain", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info("filtered out")
        logger.warning("kept")

        output = buf.getvalue()
        assert "filtered out" not in output
        assert "kept" in output


class TestJsonFormat:
    def test_json_format_emits_one_object_per_record(self):
        buf = io.StringIO()
        configure_logging(level="INFO", log_format="json", stream=buf)
        logger = logging.getLogger("wobblebot.adapters.sqlite_storage")
        logger.info("placed order")

        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "wobblebot.adapters.sqlite_storage"
        assert payload["message"] == "placed order"
        # timestamp is ISO 8601 UTC
        assert payload["timestamp"].endswith("+00:00")

    def test_json_format_surfaces_extras(self):
        buf = io.StringIO()
        configure_logging(level="INFO", log_format="json", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info(
            "trade recorded",
            extra={"order_id": "ORD-123", "symbol": "BTC/USD"},
        )

        payload = json.loads(buf.getvalue().strip())
        assert payload["order_id"] == "ORD-123"
        assert payload["symbol"] == "BTC/USD"

    def test_json_format_includes_exc_info(self):
        buf = io.StringIO()
        configure_logging(level="INFO", log_format="json", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("error during placement")

        payload = json.loads(buf.getvalue().strip())
        assert payload["level"] == "ERROR"
        assert "RuntimeError: boom" in payload["exc_info"]


class TestConfigureLoggingContract:
    def test_idempotent_no_double_handler(self):
        buf = io.StringIO()
        configure_logging(level="INFO", log_format="plain", stream=buf)
        configure_logging(level="INFO", log_format="plain", stream=buf)
        configure_logging(level="INFO", log_format="plain", stream=buf)

        logger = logging.getLogger("wobblebot.test")
        logger.info("only once")

        # Three configure calls; the message should appear exactly once.
        assert buf.getvalue().count("only once") == 1

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid log format"):
            configure_logging(log_format="yaml")  # type: ignore[arg-type]

    def test_env_var_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WOBBLEBOT_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("WOBBLEBOT_LOG_FORMAT", "json")
        buf = io.StringIO()
        configure_logging(stream=buf)

        logger = logging.getLogger("wobblebot.test")
        logger.debug("debug visible")

        payload = json.loads(buf.getvalue().strip())
        assert payload["level"] == "DEBUG"
        assert payload["message"] == "debug visible"

    def test_explicit_args_override_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WOBBLEBOT_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("WOBBLEBOT_LOG_FORMAT", "json")
        buf = io.StringIO()
        configure_logging(level="WARNING", log_format="plain", stream=buf)

        logger = logging.getLogger("wobblebot.test")
        logger.info("should be filtered")
        logger.warning("should appear")

        output = buf.getvalue()
        assert "should be filtered" not in output
        assert "[WARNING] wobblebot.test: should appear" in output


class TestRotatingFileHandler:
    """Stage 8.2.D — opt-in rotating-file log destination."""

    def test_no_file_path_keeps_stdout_only(self, tmp_path):
        """Default (rotating_file_path=None) doesn't create a file."""
        buf = io.StringIO()
        configure_logging(log_format="plain", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info("stream-only")
        # No file written anywhere under tmp_path.
        files = list(tmp_path.iterdir())
        assert files == []

    def test_writes_to_rotating_file_when_set(self, tmp_path):
        log_path = tmp_path / "wobblebot.log"
        buf = io.StringIO()
        configure_logging(log_format="plain", stream=buf, rotating_file_path=log_path)
        logger = logging.getLogger("wobblebot.test")
        logger.info("hello to the rotated log")
        # File exists + has the message (handler flushes on emit).
        for h in logging.getLogger("wobblebot").handlers:
            h.flush()
        assert log_path.exists()
        text = log_path.read_text(encoding="utf-8")
        assert "hello to the rotated log" in text
        # Stream handler still got it too.
        assert "hello to the rotated log" in buf.getvalue()

    def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "logs" / "deep" / "maintenance.log"
        assert not nested.parent.exists()
        configure_logging(rotating_file_path=nested)
        assert nested.parent.exists()

    def test_idempotent_replaces_rotating_handler(self, tmp_path):
        """Calling twice replaces the rotating handler (no stacking)."""
        log1 = tmp_path / "first.log"
        log2 = tmp_path / "second.log"
        configure_logging(rotating_file_path=log1)
        configure_logging(rotating_file_path=log2)
        root = logging.getLogger("wobblebot")
        rotating_handlers = [h for h in root.handlers if h.get_name() == "wobblebot.rotating-file"]
        # Only one rotating handler installed.
        assert len(rotating_handlers) == 1

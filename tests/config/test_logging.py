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
    """
    logger = logging.getLogger("wobblebot")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    yield
    logger.handlers = original_handlers
    logger.level = original_level
    logger.propagate = original_propagate


class TestPlainFormat:
    def test_plain_format_emits_human_readable_line(self):
        buf = io.StringIO()
        configure_logging(level="INFO", format="plain", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info("starting up")

        output = buf.getvalue().strip()
        # Format: "<asctime> [INFO] wobblebot.test: starting up"
        assert re.match(r".*\[INFO\] wobblebot\.test: starting up$", output)

    def test_plain_format_respects_level(self):
        buf = io.StringIO()
        configure_logging(level="WARNING", format="plain", stream=buf)
        logger = logging.getLogger("wobblebot.test")
        logger.info("filtered out")
        logger.warning("kept")

        output = buf.getvalue()
        assert "filtered out" not in output
        assert "kept" in output


class TestJsonFormat:
    def test_json_format_emits_one_object_per_record(self):
        buf = io.StringIO()
        configure_logging(level="INFO", format="json", stream=buf)
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
        configure_logging(level="INFO", format="json", stream=buf)
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
        configure_logging(level="INFO", format="json", stream=buf)
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
        configure_logging(level="INFO", format="plain", stream=buf)
        configure_logging(level="INFO", format="plain", stream=buf)
        configure_logging(level="INFO", format="plain", stream=buf)

        logger = logging.getLogger("wobblebot.test")
        logger.info("only once")

        # Three configure calls; the message should appear exactly once.
        assert buf.getvalue().count("only once") == 1

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid log format"):
            configure_logging(format="yaml")  # type: ignore[arg-type]

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
        configure_logging(level="WARNING", format="plain", stream=buf)

        logger = logging.getLogger("wobblebot.test")
        logger.info("should be filtered")
        logger.warning("should appear")

        output = buf.getvalue()
        assert "should be filtered" not in output
        assert "[WARNING] wobblebot.test: should appear" in output

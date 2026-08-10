"""The logging-conventions scanner, plus the regression guard it enables.

Two jobs here:

1. Pin the scanner's own behavior on synthetic snippets, so a future
   edit can't quietly stop detecting things.
2. Assert the real package reports **zero** rule-1 violations. That is
   the actual guard — the audit closed at zero across three
   installments (P3 slices 9/16/17) and this is what stops it drifting
   back one convenient log line at a time.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from tools.scan_logging import scan_decimal, scan_rule1

pytestmark = pytest.mark.unit

_PACKAGE_ROOT = pathlib.Path("src/wobblebot")


def _write(tmp_path: pathlib.Path, source: str) -> pathlib.Path:
    (tmp_path / "mod.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


class TestRule1Detection:
    def test_flags_static_message_with_extra(self, tmp_path: pathlib.Path) -> None:
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.warning("something failed", extra={"symbol": "BTC/USD"})
            """,
        )
        findings = scan_rule1(root)
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert "symbol" in findings[0].detail

    def test_interpolated_message_is_clean(self, tmp_path: pathlib.Path) -> None:
        """The fix: data in the message means no finding."""
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.warning("%s failed", symbol, extra={"symbol": "BTC/USD"})
            """,
        )
        assert scan_rule1(root) == []

    def test_static_message_without_extra_is_clean(self, tmp_path: pathlib.Path) -> None:
        """A bare status line carries no stranded data."""
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.info("session start")
            """,
        )
        assert scan_rule1(root) == []

    def test_ignores_non_logger_calls(self, tmp_path: pathlib.Path) -> None:
        root = _write(
            tmp_path,
            """
            tracker.info("something", extra={"a": 1})
            """,
        )
        assert scan_rule1(root) == []

    def test_survives_an_unparseable_file(self, tmp_path: pathlib.Path) -> None:
        """A syntax error somewhere must not abort the whole audit."""
        (tmp_path / "broken.py").write_text("def (:", encoding="utf-8")
        (tmp_path / "ok.py").write_text(
            "import logging\n_LOGGER = logging.getLogger(__name__)\n"
            '_LOGGER.error("boom", extra={"x": 1})\n',
            encoding="utf-8",
        )
        assert len(scan_rule1(tmp_path)) == 1


class TestDecimalDetection:
    def test_flags_unwrapped_money_value(self, tmp_path: pathlib.Path) -> None:
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.warning("avg cost %s", assessment.average_cost)
            """,
        )
        findings = scan_decimal(root)
        assert len(findings) == 1
        assert "average_cost" in findings[0].detail

    def test_formatted_value_is_clean(self, tmp_path: pathlib.Path) -> None:
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.warning("avg cost %s", fmt_decimal(assessment.average_cost))
            """,
        )
        assert scan_decimal(root) == []

    def test_does_not_flag_the_message_itself(self, tmp_path: pathlib.Path) -> None:
        """A money word in the MESSAGE is not an unformatted value."""
        root = _write(
            tmp_path,
            """
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.info("balance check complete")
            """,
        )
        assert scan_decimal(root) == []


class TestPackageIsClean:
    """The guard. Do not weaken these to make a change land."""

    def test_no_rule1_violations_remain(self) -> None:
        """Every log message must answer what/which/how-much on its own.

        Closed at zero by P3 slices 9/16/17. A new finding here means a
        log call put its data in ``extra=`` only — invisible to an
        operator tailing the container log, which is the exact failure
        that made three separate incidents hard to diagnose.

        Fix the log line, not this test. See
        docs/implementation/logging-conventions.md.
        """
        findings = scan_rule1(_PACKAGE_ROOT)
        assert findings == [], "rule-1 regressions:\n" + "\n".join(str(f) for f in findings)

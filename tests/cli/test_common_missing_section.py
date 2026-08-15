"""Pin the fleet-wide missing-section contract (cli/_common).

Exit code 2 on a missing per-CLI section is what the deprived-env
walkthrough in CLAUDE.md verifies across all sixteen entry points. It
used to be enforced by seventeen hand-copied three-line blocks — one of
which had drifted (screener wrote to raw stderr) — so the contract now
lives in ONE helper, and this file is where its shape is pinned.
"""

from __future__ import annotations

import logging

import pytest

from wobblebot.cli._common import missing_section_exit

pytestmark = pytest.mark.unit


class TestMissingSectionExit:
    def test_returns_the_deprived_env_exit_code(self) -> None:
        assert missing_section_exit(logging.getLogger("t"), "live") == 2

    def test_message_names_the_section_and_the_template(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The section name tells the operator WHAT is missing; the
        settings.example.yml pointer tells them WHERE to get it. Before
        extraction only two of seventeen sites carried the pointer."""
        logger = logging.getLogger("wobblebot.test.missing_section")
        with caplog.at_level(logging.ERROR, logger=logger.name):
            missing_section_exit(logger, "harvester")
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        assert "missing the `harvester:` section" in record.getMessage()
        assert "config/settings.example.yml" in record.getMessage()

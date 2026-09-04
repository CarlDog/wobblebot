"""The capital report's emit path — what the operator is actually told.

WHY THIS FILE EXISTS. Until 2026-09-04 there was no test module for
``cli/maintenance_capital.py`` at all, and the 2.0.5..main pre-deploy review
found a defect that only that gap could hide: the loop logging
sellable-but-committed positions sat BELOW ``_emit``'s clean-report early
return, so in exactly the case it was written for the line was built and
thrown away while the operator was told "capital report clean".

Six service-level tests and a 4-of-4 mutation pass had gone green over that
bug, because every one of them called the pure functions in
``services/capital_reporter.py`` and none of them called ``_emit`` — the
function that decides what is logged and what is notified. Three independent
review dimensions found it, and the completeness critic reproduced it by
EXECUTING ``_emit`` rather than reading it.

So these tests drive the real ``_emit``.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from wobblebot.cli.maintenance_capital import _emit
from wobblebot.domain.value_objects import PairLimits, Symbol
from wobblebot.services.capital_reporter import CapitalReport, check_exit_viability

pytestmark = pytest.mark.unit

_DOGE = Symbol(base="DOGE", quote="USD")
_LIMITS = PairLimits(symbol=_DOGE, ordermin=Decimal("50"), costmin=Decimal("0.5"))


def _committed_only() -> CapitalReport:
    """DOGE as the live account actually held it on 2026-09-04: 293.64
    total, 31.54 free, ordermin 50. Sellable, merely committed — the case
    the whole total/available split was written for."""
    return CapitalReport(
        entry=(),
        exits=(
            check_exit_viability(
                _DOGE,
                total_quantity=Decimal("293.64029343"),
                available_quantity=Decimal("31.54235586"),
                limits=_LIMITS,
                source="exchange",
            ),
        ),
        cap=None,
    )


class _RecordingNotifier:
    """Fails the test if the report tries to page for a committed position."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


@pytest.mark.asyncio
class TestCommittedPositionsAreLoggedButNeverNotified:
    async def test_the_informational_line_is_actually_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The defect, pinned. A committed position is excluded from the
        warning set by construction, so if the informational loop sits below
        the clean-report early return this line never reaches the log."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.maintenance"):
            rc = await _emit(_committed_only(), None)

        assert rc == 0
        informational = [r for r in caplog.records if "CAPITAL (informational)" in r.getMessage()]
        assert informational, (
            "a committed position produced no informational line. Three docstrings "
            "promise it is 'logged, never notified'; if the summarize_committed loop "
            "sits below the `if not lines:` early return it is dead code in exactly "
            "the case it exists for."
        )
        message = informational[0].getMessage()
        assert "293.64029343" in message and "31.54235586" in message
        assert "committed to resting orders" in message

    async def test_it_is_not_a_warning_and_does_not_page(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other half. Before the split this position produced a daily
        WARNING plus a notification, saying it could not be sold at all."""
        notifier = _RecordingNotifier()
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.maintenance"):
            await _emit(_committed_only(), notifier)  # type: ignore[arg-type]

        assert not notifier.calls, "a committed position must never page the operator"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a sellable-but-committed position is the steady state of a working "
            "grid and must not be logged at WARNING"
        )

    async def test_the_clean_line_says_how_many_were_reported_informationally(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ "Clean" must not read as "nothing to see". It is true — nothing
        needs action — but the operator should be able to tell the difference
        between a silent cycle and one that reported something."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.maintenance"):
            await _emit(_committed_only(), None)

        clean = [r for r in caplog.records if "capital report clean" in r.getMessage()]
        assert clean, "the clean-report line must still be emitted"
        assert getattr(clean[0], "committed_positions", None) == 1

    async def test_a_genuinely_empty_report_reports_zero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The contrast case, so the count above is not vacuously satisfied."""
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.maintenance"):
            await _emit(CapitalReport(entry=(), exits=(), cap=None), None)

        clean = [r for r in caplog.records if "capital report clean" in r.getMessage()]
        assert clean
        assert getattr(clean[0], "committed_positions", None) == 0
        assert not [r for r in caplog.records if "CAPITAL (informational)" in r.getMessage()]

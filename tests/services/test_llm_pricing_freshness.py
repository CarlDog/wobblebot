"""Stale-pricing watchdog (Stage 6.1.B / ADR-014 decision 6).

Fails CI when any entry in ``services/llm_pricing.py`` has a
``verified_date`` more than 180 days behind today. Forces a periodic
re-verification decision — either bump the date (after re-checking
the provider's pricing page) or commit a deliberate suppression.
The point is to make staleness loud, not to embargo a single PR.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wobblebot.services.llm_pricing import all_price_points

pytestmark = pytest.mark.unit

_MAX_STALE_DAYS = 180


def test_no_price_point_is_stale() -> None:
    today = date.today()
    threshold = today - timedelta(days=_MAX_STALE_DAYS)
    stale = [p for p in all_price_points() if p.verified_date < threshold]
    if stale:
        lines = [
            f"  - {p.provider}/{p.model}: verified {p.verified_date} "
            f"({(today - p.verified_date).days} days ago)"
            for p in stale
        ]
        pytest.fail(
            "The following pricing entries in services/llm_pricing.py "
            f"are >{_MAX_STALE_DAYS} days old. Verify against the "
            "provider's pricing-page URL (in the source comments) and "
            "bump verified_date:\n" + "\n".join(lines)
        )

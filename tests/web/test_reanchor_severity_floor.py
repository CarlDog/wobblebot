"""`WebConfig.reanchor_min_severity` — the operator's banner attention floor.

The floor is only defensible because of a property of the CLASSIFIER, not
of the config: severity self-escalates on age. The dashboard banner is the
ONLY surface a re-anchor recommendation reaches — there is no notification
event for it, no Discord message, and the heuristic advisor never emits one
(it mentions re-anchoring in prose only). So if `_classify_reanchor_severity`
ever stopped escalating on age, a floor above "mild" would stop being a
DELAY and silently become signal DELETION.

`TestAgeEscalationInvariant` pins that property. A failure there is a reason
to remove the floor, not to relax the test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import REANCHOR_SEVERITY_ORDER, WebConfig
from wobblebot.domain.grid import GridState
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.web.routes.status_reanchor import (
    _classify_reanchor_severity,
    load_reanchor_recommendations,
)

_BTC = Symbol(base="BTC", quote="USD")

# Reference 30000 at 1% spacing => one spacing is $300.
# Drift is measured to the nearest OPEN ORDER, which sits at 30000.
# 2.0 spacings lands mid-"mild" band (>=1.5, <2.5) with room either side,
# so an off-by-a-hair threshold change doesn't silently reclassify it.
_MILD_PRICE = Decimal("30600")
# 3.0 spacings -> drift_tier 2 -> "moderate" at any age.
_MODERATE_PRICE = Decimal("30900")
# 5.0 spacings -> drift_tier 3 -> "strong" at any age.
_STRONG_PRICE = Decimal("31500")

_HOUR = 3600


@pytest_asyncio.fixture
async def live_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _make_order(price: str = "30000") -> Order:
    return Order(
        id=uuid4(),
        exchange_id="ABC-123",
        symbol=_BTC,
        side="buy",
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


async def _seed_drifted_grid(storage: SQLiteStorageAdapter) -> Order:
    await storage.save_grid_state(
        GridState(
            symbol=_BTC,
            reference_price=Decimal("30000"),
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    return _make_order()


class TestAgeEscalationInvariant:
    """Age alone must escalate severity — the property the floor rests on.

    Drift is held constant in the mild band across every case, so any tier
    change here is attributable to age and nothing else.
    """

    @pytest.mark.parametrize(
        ("age_hours", "expected"),
        [
            (0, "mild"),
            (23, "mild"),
            (24, "mild"),  # age_tier 1 == drift_tier 1; max() is still 1
            (47, "mild"),
            (48, "moderate"),  # age_tier 2 overtakes drift_tier 1
            (71, "moderate"),
            (72, "strong"),  # age_tier 3
            (24 * 30, "strong"),
        ],
    )
    def test_mild_drift_escalates_on_age_alone(self, age_hours: int, expected: str) -> None:
        # 2.0 spacings: squarely mild on drift, for every age below.
        assert _classify_reanchor_severity(2.0, age_hours * _HOUR) == expected

    def test_a_persisting_mild_finding_always_reaches_moderate(self) -> None:
        """The exact guarantee the floor's safety argument depends on.

        For every drift that produces a banner at all, waiting long enough
        yields at least "moderate" — so a filtered mild finding resurfaces
        rather than vanishing. Stated as a sweep, not one example, because
        the claim is universal over the mild band.
        """
        mild_band = [1.5, 1.75, 2.0, 2.25, 2.49]
        for drift in mild_band:
            assert _classify_reanchor_severity(drift, 0) == "mild"
            escalated = _classify_reanchor_severity(drift, 48 * _HOUR)
            assert escalated is not None
            assert escalated != "mild", f"drift {drift} failed to escalate on age"

    def test_age_cannot_manufacture_a_banner_without_drift(self) -> None:
        """The converse guard: drift is still the gate.

        A calm market with an ancient parked grid must stay silent, or
        raising the floor would be masking banners that should never have
        fired.
        """
        assert _classify_reanchor_severity(0.5, 24 * 365 * _HOUR) is None


@pytest.mark.asyncio
class TestSeverityFloorFiltering:
    async def _recs(
        self, storage: SQLiteStorageAdapter, price: Decimal, min_severity: str = "mild"
    ) -> tuple:
        order = await _seed_drifted_grid(storage)
        return await load_reanchor_recommendations(
            storage,
            [order],
            {_BTC: price},
            {str(order.id): 0},
            set(),
            {},
            None,
            None,
            min_severity,  # type: ignore[arg-type]
        )

    async def test_default_floor_shows_mild(self, live_storage: SQLiteStorageAdapter) -> None:
        """Default is the pre-knob behavior: nothing is filtered."""
        recs = await self._recs(live_storage, _MILD_PRICE)
        assert [r.severity for r in recs] == ["mild"]

    async def test_moderate_floor_suppresses_mild(self, live_storage: SQLiteStorageAdapter) -> None:
        recs = await self._recs(live_storage, _MILD_PRICE, min_severity="moderate")
        assert recs == ()

    async def test_moderate_floor_admits_moderate(self, live_storage: SQLiteStorageAdapter) -> None:
        recs = await self._recs(live_storage, _MODERATE_PRICE, min_severity="moderate")
        assert [r.severity for r in recs] == ["moderate"]

    async def test_moderate_floor_never_hides_strong(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        """A floor must not suppress anything ABOVE it — the failure mode
        a naive equality comparison would introduce."""
        recs = await self._recs(live_storage, _STRONG_PRICE, min_severity="moderate")
        assert [r.severity for r in recs] == ["strong"]

    async def test_strong_floor_suppresses_moderate(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        recs = await self._recs(live_storage, _MODERATE_PRICE, min_severity="strong")
        assert recs == ()


class TestConfigSurface:
    def test_default_is_show_everything(self) -> None:
        """Adding the knob must not change any existing deployment."""
        assert WebConfig().reanchor_min_severity == "mild"

    def test_schema_rejects_an_unknown_tier(self) -> None:
        with pytest.raises(ValueError):
            WebConfig(reanchor_min_severity="critical")  # type: ignore[arg-type]

    def test_every_classifier_output_is_rankable(self) -> None:
        """`REANCHOR_SEVERITY_ORDER` must cover every tier the classifier
        can return, or the floor comparison raises KeyError in production
        on whichever tier was forgotten."""
        produced = {
            _classify_reanchor_severity(drift, age * _HOUR)
            for drift in (1.6, 3.0, 5.0)
            for age in (0, 48, 72)
        }
        produced.discard(None)
        assert produced == set(REANCHOR_SEVERITY_ORDER)

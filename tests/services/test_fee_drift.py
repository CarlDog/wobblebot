"""ADR-038 — the engine's per-fill fee-drift tripwire."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.services.grid_engine import GridEngine

pytestmark = pytest.mark.unit

BTC_USD = Symbol(base="BTC", quote="USD")


def _trade(*, cost: str, fee: str) -> Trade:
    from datetime import UTC, datetime

    return Trade(
        id="T-1",
        order_id="O-1",
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal("0.0001"), asset="BTC"),
        fee=Decimal(fee),
        cost=Decimal(cost),
        executed_at=Timestamp(dt=datetime.now(UTC)),
    )


def _engine(maker: str = "0.0040", taker: str = "0.0080") -> GridEngine:
    """Engine with only what the tripwire touches — a bare instance is
    enough because _check_fee_drift reads only the rate fields and the
    counter dict."""
    engine = GridEngine.__new__(GridEngine)
    engine._maker_fee_rate = Decimal(maker)  # pylint: disable=protected-access
    engine._taker_fee_rate = Decimal(taker)  # pylint: disable=protected-access
    engine._fee_anomaly_counts = {}  # pylint: disable=protected-access
    return engine


class TestFeeDriftTripwire:
    def test_maker_rate_fill_is_clean(self) -> None:
        engine = _engine()
        engine._check_fee_drift(BTC_USD, _trade(cost="5.00", fee="0.0200"))  # 0.40%
        assert engine.fee_anomaly_count(BTC_USD) == 0

    def test_taker_rate_fill_is_clean(self) -> None:
        engine = _engine()
        engine._check_fee_drift(BTC_USD, _trade(cost="5.00", fee="0.0400"))  # 0.80%
        assert engine.fee_anomaly_count(BTC_USD) == 0

    def test_doubled_schedule_fill_trips(self, caplog: pytest.LogCaptureFixture) -> None:
        """The 2026-07-13 scenario: engine believes 0.25/0.40, Kraken
        bills 0.80% — must trip on the FIRST fill."""
        engine = _engine(maker="0.0025", taker="0.0040")
        with caplog.at_level(logging.WARNING, logger="wobblebot.services.grid_engine"):
            engine._check_fee_drift(BTC_USD, _trade(cost="5.00", fee="0.0400"))  # 0.80%
        assert engine.fee_anomaly_count(BTC_USD) == 1
        assert any("fee drift" in r.message for r in caplog.records)

    def test_tolerance_absorbs_rounding(self) -> None:
        """4 bps off maker stays inside the 5 bps tolerance."""
        engine = _engine()
        engine._check_fee_drift(BTC_USD, _trade(cost="5.00", fee="0.0220"))  # 0.44%
        assert engine.fee_anomaly_count(BTC_USD) == 0

    def test_zero_cost_is_ignored(self) -> None:
        engine = _engine()
        engine._check_fee_drift(BTC_USD, _trade(cost="0", fee="0"))
        assert engine.fee_anomaly_count(BTC_USD) == 0

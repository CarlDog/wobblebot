"""Tests for the cost dashboard (Stage 7.2.A)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.cost import (
    _empty_fees_snapshot,
    _empty_honesty_snapshot,
    _empty_snapshot,
    _load_trading_fees_snapshot,
    _rollup,
    _rollup_fees,
    _rollup_honesty,
)

pytestmark = pytest.mark.unit


def _row(
    *,
    cost: str,
    role: str = "operator",
    provider: str = "anthropic",
    hours_ago: float = 1.0,
) -> LLMCallRecord:
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    return LLMCallRecord(
        id=uuid4(),
        timestamp=Timestamp(dt=when),
        role=role,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        tokens_in=10,
        tokens_out=20,
        cost_usd=Decimal(cost),
        success=True,
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


@pytest.fixture
def client(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


# --------------------------------------------------------------------- #
# Rollup logic (pure)                                                   #
# --------------------------------------------------------------------- #


class TestRollup:
    def test_empty_input_returns_zero_snapshot(self) -> None:
        snap = _rollup([], now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0")
        assert snap.total_7d_usd == Decimal("0")
        assert snap.call_count_24h == 0
        assert snap.call_count_7d == 0
        assert snap.per_day == ()
        assert snap.per_provider_role == ()

    def test_groups_by_provider_role(self) -> None:
        rows = [
            _row(cost="0.001", provider="anthropic", role="operator"),
            _row(cost="0.002", provider="anthropic", role="operator"),
            _row(cost="0.0005", provider="openai", role="quant"),
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0.0035")
        assert snap.call_count_24h == 3
        # Sorted by cost desc
        assert snap.per_provider_role[0].key == "anthropic / operator"
        assert snap.per_provider_role[0].cost_usd == Decimal("0.003")
        assert snap.per_provider_role[1].key == "openai / quant"

    def test_24h_window_excludes_older_rows(self) -> None:
        rows = [
            _row(cost="0.001", hours_ago=1),
            _row(cost="0.005", hours_ago=48),
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.total_24h_usd == Decimal("0.001")
        assert snap.total_7d_usd == Decimal("0.006")
        assert snap.call_count_24h == 1
        assert snap.call_count_7d == 2

    def test_per_day_sorted_desc(self) -> None:
        now = datetime.now(UTC)
        rows = [
            _row(cost="0.001", hours_ago=1),  # today
            _row(cost="0.002", hours_ago=24 + 1),  # yesterday
            _row(cost="0.003", hours_ago=48 + 1),  # day-before
        ]
        snap = _rollup(rows, now=now)
        days = [d.day for d in snap.per_day]
        assert days == sorted(days, reverse=True)
        assert len(snap.per_day) == 3

    def test_cached_tokens_summed_over_24h_window_only(self) -> None:
        rows = [
            _row(cost="0.001", hours_ago=1).model_copy(update={"tokens_cache_read": 1024}),
            _row(cost="0.001", hours_ago=2).model_copy(update={"tokens_cache_read": 500}),
            # Outside the 24h window — excluded from the cached sum.
            _row(cost="0.001", hours_ago=48).model_copy(update={"tokens_cache_read": 9999}),
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.cached_tokens_24h == 1524

    def test_cached_tokens_zero_when_no_cache_hits(self) -> None:
        snap = _rollup([_row(cost="0.001")], now=datetime.now(UTC))
        assert snap.cached_tokens_24h == 0


# --------------------------------------------------------------------- #
# Routes                                                                #
# --------------------------------------------------------------------- #


class TestCostRoute:
    def test_anonymous_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/cost")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

    def test_authenticated_empty_renders(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/cost")
        assert resp.status_code == 200
        assert "Cost" in resp.text
        assert "No cloud LLM calls" in resp.text  # empty-state copy

    @pytest.mark.asyncio
    async def test_authenticated_with_data_renders_rollup(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        # Seed the operator first.
        # The fixture's already seeded a user; we just need to add rows.
        await storage.save_llm_call(_row(cost="0.001234"))
        await storage.save_llm_call(_row(cost="0.000567", provider="openai"))

        # Use a separate client since the per-fixture client doesn't
        # see async-test seeding without explicit fixture composition.
        from fastapi.testclient import TestClient

        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            resp = client.get("/cost")
            assert resp.status_code == 200
            assert "0.001234" in resp.text
            assert "anthropic / operator" in resp.text
            assert "openai / operator" in resp.text
            assert 'class="bar-chart"' in resp.text  # 7-day spend bars render
            # No cache hits seeded → the cached-tokens meta line hides.
            assert "cached input tokens" not in resp.text

    @pytest.mark.asyncio
    async def test_cached_tokens_render_in_card_meta(self, storage: SQLiteStorageAdapter) -> None:
        """ADR-033: a row with cache hits surfaces the 24h cached-token
        count in the LLM card's meta line."""
        await storage.save_llm_call(
            _row(cost="0.001").model_copy(update={"tokens_cache_read": 2048})
        )

        from fastapi.testclient import TestClient

        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            resp = client.get("/cost")
            assert resp.status_code == 200
            assert "2,048 cached input tokens" in resp.text


class TestCostCardFragment:
    def test_anonymous_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/cost/card")
        assert resp.status_code == 302

    def test_authenticated_returns_fragment(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/cost/card")
        assert resp.status_code == 200
        assert "cost-card" in resp.text
        # No full-page chrome — no nav links inside the fragment.
        assert "Sign out" not in resp.text


# --------------------------------------------------------------------- #
# _empty_snapshot                                                       #
# --------------------------------------------------------------------- #


class TestEmptySnapshot:
    def test_carries_error_string(self) -> None:
        snap = _empty_snapshot(error="db down")
        assert snap.error == "db down"
        assert snap.total_24h_usd == Decimal("0")

    def test_default_no_error(self) -> None:
        snap = _empty_snapshot()
        assert snap.error is None


# --------------------------------------------------------------------- #
# Stage 8.4 follow-up: trading-fees rollup                              #
# --------------------------------------------------------------------- #


def _trade(
    *,
    fee: str,
    hours_ago: float = 1.0,
    symbol_base: str = "BTC",
) -> "Trade":
    """Construct a Trade row for the rollup tests."""
    from wobblebot.domain.models import Trade
    from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol

    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    return Trade(
        id=f"TRADE-{uuid4().hex[:12]}",
        order_id=f"ORDER-{uuid4().hex[:12]}",
        symbol=Symbol(base=symbol_base, quote="USD"),
        side=OrderSide.SELL,
        price=Price(amount=Decimal("77000"), currency="USD"),
        amount=Amount(value=Decimal("0.00013"), asset=symbol_base),
        fee=Decimal(fee),
        cost=Decimal("10.00"),
        executed_at=Timestamp(dt=when),
    )


class TestEmptyFeesSnapshot:
    def test_unwired_default(self) -> None:
        snap = _empty_fees_snapshot(wired=False)
        assert snap.live_wired is False
        assert snap.total_all_time_usd == Decimal("0")
        assert snap.error is None

    def test_wired_with_error(self) -> None:
        snap = _empty_fees_snapshot(wired=True, error="storage down")
        assert snap.live_wired is True
        assert snap.error == "storage down"


class TestRollupFees:
    def test_empty_trades_zero_totals(self) -> None:
        snap = _rollup_fees([], now=datetime.now(UTC))
        assert snap.live_wired is True
        assert snap.total_24h_usd == Decimal("0")
        assert snap.total_7d_usd == Decimal("0")
        assert snap.total_30d_usd == Decimal("0")
        assert snap.total_all_time_usd == Decimal("0")
        assert snap.trade_count_all_time == 0

    def test_bucket_by_window(self) -> None:
        now = datetime.now(UTC)
        trades = [
            _trade(fee="0.025", hours_ago=2),  # 24h window
            _trade(fee="0.030", hours_ago=24 * 3),  # 7d window (not 24h)
            _trade(fee="0.040", hours_ago=24 * 15),  # 30d window (not 7d)
            _trade(fee="0.050", hours_ago=24 * 100),  # all-time only
        ]
        snap = _rollup_fees(trades, now=now)
        assert snap.total_24h_usd == Decimal("0.025")
        assert snap.total_7d_usd == Decimal("0.055")  # 24h trade still counts
        assert snap.total_30d_usd == Decimal("0.095")  # 7d trades count too
        assert snap.total_all_time_usd == Decimal("0.145")
        assert snap.trade_count_24h == 1
        assert snap.trade_count_7d == 2
        assert snap.trade_count_30d == 3
        assert snap.trade_count_all_time == 4

    def test_nested_window_inclusion(self) -> None:
        """A trade in the 24h window MUST also be counted in 7d / 30d /
        all-time. Outer windows are supersets of inner."""
        now = datetime.now(UTC)
        snap = _rollup_fees(
            [_trade(fee="0.025", hours_ago=1)],
            now=now,
        )
        assert snap.trade_count_24h == 1
        assert snap.trade_count_7d == 1
        assert snap.trade_count_30d == 1
        assert snap.trade_count_all_time == 1


class TestLoadTradingFeesSnapshot:
    @pytest.mark.asyncio
    async def test_none_storage_returns_unwired(self) -> None:
        snap = await _load_trading_fees_snapshot(None)
        assert snap.live_wired is False
        assert snap.error is None
        assert snap.total_all_time_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero(self) -> None:
        adapter = SQLiteStorageAdapter(":memory:")
        await adapter.connect()
        try:
            snap = await _load_trading_fees_snapshot(adapter)
            assert snap.live_wired is True
            assert snap.error is None
            assert snap.total_all_time_usd == Decimal("0")
            assert snap.trade_count_all_time == 0
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_persisted_trades_sum_correctly(self) -> None:
        adapter = SQLiteStorageAdapter(":memory:")
        await adapter.connect()
        try:
            # Three trades within last 24h.
            for fee in ("0.025", "0.030", "0.020"):
                await adapter.save_trade(_trade(fee=fee, hours_ago=2))
            snap = await _load_trading_fees_snapshot(adapter)
            assert snap.live_wired is True
            assert snap.error is None
            assert snap.total_24h_usd == Decimal("0.075")
            assert snap.trade_count_24h == 3
            assert snap.total_all_time_usd == Decimal("0.075")
        finally:
            await adapter.close()


# --------------------------------------------------------------------- #
# P4.7: per-trace rollup + the cost-honesty ledger                      #
# --------------------------------------------------------------------- #


class TestTraceRollup:
    def test_groups_by_trace_with_untraced_last(self) -> None:
        rows = [
            _row(cost="0.002", role="quant").model_copy(update={"trace_id": "aaaa1111-x"}),
            _row(cost="0.001", role="quant").model_copy(update={"trace_id": "aaaa1111-x"}),
            _row(cost="0.004", role="quant").model_copy(update={"trace_id": "bbbb2222-y"}),
            _row(cost="0.003"),  # untraced (cli/operator today)
        ]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert [t.trace_label for t in snap.per_trace] == ["bbbb2222", "aaaa1111", "untraced"]
        assert snap.per_trace[0].cost_usd == Decimal("0.004")
        assert snap.per_trace[1].call_count == 2
        assert snap.per_trace[1].cost_usd == Decimal("0.003")
        assert snap.per_trace[2].roles == "operator"

    def test_rows_outside_24h_are_not_traced(self) -> None:
        rows = [_row(cost="0.001", hours_ago=30).model_copy(update={"trace_id": "old-trace"})]
        snap = _rollup(rows, now=datetime.now(UTC))
        assert snap.per_trace == ()


def _cycle(*, net_pnl: str, hours_ago: float, fee: str = "0.04") -> "RecentCycle":
    """A completed cycle whose SELL fired ``hours_ago`` hours back.

    ``net_pnl`` is taken at face value — per cycle_matcher's contract
    it is ALREADY net of both legs' fees (the ``fee`` here only
    documents that fees existed; honesty math must never touch it).
    """
    from wobblebot.domain.value_objects import Amount, Price, Symbol
    from wobblebot.services.cycle_matcher import RecentCycle

    sell_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    return RecentCycle(
        symbol=Symbol(base="BTC", quote="USD"),
        buy_executed_at=Timestamp(dt=sell_at - timedelta(hours=1)),
        sell_executed_at=Timestamp(dt=sell_at),
        buy_price=Price(amount=Decimal("60000"), currency="USD"),
        sell_price=Price(amount=Decimal("61800"), currency="USD"),
        amount=Amount(value=Decimal("0.0002"), asset="BTC"),
        buy_fee=Decimal(fee),
        sell_fee=Decimal(fee),
        net_pnl=Decimal(net_pnl),
    )


class TestRollupHonesty:
    def test_window_math_and_annualized(self) -> None:
        now = datetime.now(UTC)
        cycles = [
            _cycle(net_pnl="0.50", hours_ago=24),  # in 7d and 30d
            _cycle(net_pnl="0.25", hours_ago=24 * 20),  # 30d only
        ]
        llm = [
            _row(cost="0.10", hours_ago=2),
            _row(cost="0.05", hours_ago=24 * 10),
        ]
        snap = _rollup_honesty(cycles, llm, now=now, monthly_infra_usd=Decimal("3.00"))
        w7, w30 = snap.windows
        assert (w7.realized_pnl_usd, w7.llm_cost_usd) == (Decimal("0.50"), Decimal("0.10"))
        assert w7.infra_cost_usd == Decimal("0.7")  # 3.00 / 30 * 7
        assert w7.net_usd == Decimal("-0.3")
        assert w7.cycle_count == 1
        assert (w30.realized_pnl_usd, w30.llm_cost_usd) == (Decimal("0.75"), Decimal("0.15"))
        assert w30.infra_cost_usd == Decimal("3.00")
        assert w30.net_usd == Decimal("-2.40")
        assert snap.annualized_net_usd == Decimal("-2.40") * Decimal("365") / Decimal("30")
        assert snap.infra_declared is True

    def test_fees_are_never_double_counted(self) -> None:
        """cycle.net_pnl is already net of both legs' fees — the ledger
        must use it as-is, never subtracting trade fees again."""
        cycles = [_cycle(net_pnl="1.00", hours_ago=2, fee="5.00")]  # huge fees, already inside
        snap = _rollup_honesty(cycles, [], now=datetime.now(UTC), monthly_infra_usd=None)
        assert snap.windows[0].net_usd == Decimal("1.00")

    def test_undeclared_infra_is_none_and_excluded(self) -> None:
        snap = _rollup_honesty(
            [_cycle(net_pnl="1.00", hours_ago=2)],
            [],
            now=datetime.now(UTC),
            monthly_infra_usd=None,
        )
        assert snap.infra_declared is False
        assert all(w.infra_cost_usd is None for w in snap.windows)
        assert snap.windows[0].net_usd == Decimal("1.00")

    def test_declared_zero_is_a_real_declaration(self) -> None:
        snap = _rollup_honesty(
            [_cycle(net_pnl="1.00", hours_ago=2)],
            [],
            now=datetime.now(UTC),
            monthly_infra_usd=Decimal("0"),
        )
        assert snap.infra_declared is True
        assert snap.windows[0].infra_cost_usd == Decimal("0")

    def test_unwired_and_error_shapes(self) -> None:
        assert _empty_honesty_snapshot(wired=False).live_wired is False
        errored = _empty_honesty_snapshot(wired=True, error="db down")
        assert errored.error == "db down"
        assert errored.windows == ()


class TestCostPageHonestyCard:
    def test_card_renders_with_unwired_placeholder(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/cost")
        assert resp.status_code == 200
        assert "Cost Honesty" in resp.text
        # live_db unwired in the fixture app -> the honest ledger says so.
        assert "web.live_db" in resp.text

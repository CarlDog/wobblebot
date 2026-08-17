"""Tests for the status dashboard (Stage 7.2.B)."""

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
from wobblebot.domain.models import Balance, Order, Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.ports.operator import CommandResult, PendingCommand, ReanchorCommand
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.status import (
    _build_sparkline,
    _compute_balance_metrics,
    _load_snapshot,
    held_by_symbol,
)

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def live_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def observe_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _build_client(
    operator: SQLiteStorageAdapter,
    live: SQLiteStorageAdapter | None,
    *,
    observe: SQLiteStorageAdapter | None = None,
    cool_down_minutes: float | None = None,
    live_tick_seconds: float | None = None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        live_storage=live,
        observe_storage=observe,
        cool_down_minutes=cool_down_minutes,
        live_tick_seconds=live_tick_seconds,
    )
    return TestClient(app, follow_redirects=False)


def _make_order(*, symbol: str = "BTC/USD", side: str = "buy", price: str = "30000") -> Order:
    base, quote = symbol.split("/")
    return Order(
        id=uuid4(),
        exchange_id="ABC-123",
        symbol=Symbol(base=base, quote=quote),
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal(price), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status="open",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _make_trade(*, symbol: str = "BTC/USD", side: str = "buy") -> Trade:
    base, quote = symbol.split("/")
    return Trade(
        id="TXID-" + uuid4().hex[:8],
        order_id="OID-" + uuid4().hex[:8],
        symbol=Symbol(base=base, quote=quote),
        side=side,  # type: ignore[arg-type]
        price=Price(amount=Decimal("30000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        fee=Decimal("0.12"),
        cost=Decimal("30.00"),
        executed_at=Timestamp(dt=datetime.now(UTC) - timedelta(seconds=10)),
    )


# --------------------------------------------------------------------- #
# Account scoreboard                                                    #
# --------------------------------------------------------------------- #


def _make_cycle_trades() -> tuple[Trade, Trade]:
    """A matched BUY->SELL pair (cheaper buy, same amount) = one cycle.

    net = (31000-30000) * 0.001 - 0.10 - 0.10 = 0.80
    """
    now = datetime.now(UTC)
    buy = Trade(
        id="TXID-buy",
        order_id="OID-buy",
        symbol=Symbol(base="BTC", quote="USD"),
        side="buy",  # type: ignore[arg-type]
        price=Price(amount=Decimal("30000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        fee=Decimal("0.10"),
        cost=Decimal("30.00"),
        executed_at=Timestamp(dt=now - timedelta(minutes=5)),
    )
    sell = Trade(
        id="TXID-sell",
        order_id="OID-sell",
        symbol=Symbol(base="BTC", quote="USD"),
        side="sell",  # type: ignore[arg-type]
        price=Price(amount=Decimal("31000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        fee=Decimal("0.10"),
        cost=Decimal("31.00"),
        executed_at=Timestamp(dt=now - timedelta(minutes=1)),
    )
    return buy, sell


class TestScoreboard:
    def test_compute_balance_metrics_values_held_inventory(self) -> None:
        balances = [
            Balance(
                asset="USD", total=Decimal("100"), available=Decimal("80"), locked=Decimal("20")
            ),
            Balance(
                asset="BTC", total=Decimal("0.001"), available=Decimal("0.001"), locked=Decimal("0")
            ),
        ]
        prices = {Symbol(base="BTC", quote="USD"): Decimal("50000")}
        free, account, held = _compute_balance_metrics(balances, prices)
        assert free == Decimal("80")
        assert held == Decimal("50")  # 0.001 * 50000
        assert account == Decimal("150")  # 100 USD + 50 held

    def test_compute_balance_metrics_empty_is_none(self) -> None:
        assert _compute_balance_metrics([], {}) == (None, None, None)

    def test_compute_balance_metrics_skips_unpriced_held(self) -> None:
        # A held asset with no observed price is omitted (undercount), not
        # a crash — the USD + priced assets still value.
        balances = [
            Balance(
                asset="USD", total=Decimal("100"), available=Decimal("100"), locked=Decimal("0")
            ),
            Balance(
                asset="DOGE", total=Decimal("500"), available=Decimal("500"), locked=Decimal("0")
            ),
        ]
        assert _compute_balance_metrics(balances, {}) == (
            Decimal("100"),
            Decimal("100"),
            Decimal("0"),
        )

    @pytest.mark.asyncio
    async def test_scoreboard_renders_balance_and_lifetime_pnl(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            await observe.save_balance_snapshot(
                [
                    Balance(
                        asset="USD",
                        total=Decimal("159.95"),
                        available=Decimal("159.95"),
                        locked=Decimal("0"),
                    )
                ]
            )
            buy, sell = _make_cycle_trades()
            await live_storage.save_trade(buy)
            await live_storage.save_trade(sell)
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert 'class="scoreboard"' in resp.text
                assert "account value" in resp.text
                assert "159.95" in resp.text  # account value == free USD (no held)
                assert "lifetime PnL" in resp.text
                assert "+$0.80" in resp.text  # cycle net (house usd_signed rendering)
        finally:
            await observe.close()

    @pytest.mark.asyncio
    async def test_scoreboard_degrades_without_observe_db(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        # observe.db unwired -> PnL still shows (from live.db), money cells
        # degrade to an em-dash rather than 500ing.
        buy, sell = _make_cycle_trades()
        await live_storage.save_trade(buy)
        await live_storage.save_trade(sell)
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert 'class="scoreboard"' in resp.text
            assert "lifetime PnL" in resp.text
            assert "—" in resp.text  # money cells degraded

    @pytest.mark.asyncio
    async def test_parked_symbol_shows_price(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        # A symbol with a recent trade but NO open order still shows its
        # price on the card (the BTC-offside "bare name, no price" gap).
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            eth = Symbol(base="ETH", quote="USD")
            await observe.save_price_snapshot(
                eth,
                Price(amount=Decimal("1800.00"), currency="USD"),
                Timestamp(dt=datetime.now(UTC)),
            )
            # ETH SELL with no matching BUY -> ETH appears via recent_trades
            # with no open orders (parked).
            await live_storage.save_trade(
                Trade(
                    id="TXID-eth",
                    order_id="OID-eth",
                    symbol=eth,
                    side="sell",  # type: ignore[arg-type]
                    price=Price(amount=Decimal("1810"), currency="USD"),
                    amount=Amount(value=Decimal("0.01"), asset="ETH"),
                    fee=Decimal("0.05"),
                    cost=Decimal("18.10"),
                    executed_at=Timestamp(dt=datetime.now(UTC) - timedelta(seconds=30)),
                )
            )
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert "ETH/USD" in resp.text
                assert "No open orders for this symbol." in resp.text  # parked
                assert "symbol-price" in resp.text  # price rendered on the card
                assert "$1,800.00" in resp.text  # the fetched price (house usd rendering)
        finally:
            await observe.close()


# --------------------------------------------------------------------- #
# Per-symbol sparkline                                                  #
# --------------------------------------------------------------------- #


class TestSparkline:
    def test_under_two_points_is_none(self) -> None:
        assert _build_sparkline([Decimal("100")], None, None, Decimal("100")) is None

    def test_geometry_and_inside_band(self) -> None:
        spark = _build_sparkline(
            [Decimal("100"), Decimal("102"), Decimal("101")],
            Decimal("99"),
            Decimal("103"),
            Decimal("101"),
        )
        assert spark is not None
        assert spark.points  # non-empty "x,y x,y ..."
        assert spark.band_y is not None and spark.band_h is not None
        assert spark.offside is False  # 101 within [99, 103]

    def test_offside_when_current_outside_band(self) -> None:
        spark = _build_sparkline(
            [Decimal("100"), Decimal("110")], Decimal("95"), Decimal("105"), Decimal("110")
        )
        assert spark is not None
        assert spark.offside is True  # 110 > 105 -> parked

    def test_no_band_without_orders(self) -> None:
        spark = _build_sparkline([Decimal("100"), Decimal("101")], None, None, Decimal("101"))
        assert spark is not None
        assert spark.band_y is None
        assert spark.offside is False

    @pytest.mark.asyncio
    async def test_sparkline_renders_with_price_series(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            eth = Symbol(base="ETH", quote="USD")
            now = datetime.now(UTC)
            for i, p in enumerate(["1800", "1810", "1805"]):
                await observe.save_price_snapshot(
                    eth,
                    Price(amount=Decimal(p), currency="USD"),
                    Timestamp(dt=now - timedelta(minutes=30 - i * 10)),
                )
            await live_storage.save_order(_make_order(symbol="ETH/USD", price="1790"))
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert 'class="sparkline' in resp.text
                assert "spark-line" in resp.text
        finally:
            await observe.close()

    @pytest.mark.asyncio
    async def test_per_order_delta_column(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            eth = Symbol(base="ETH", quote="USD")
            # Current market 1810; a BUY at 1790 is +1.12% below market.
            await observe.save_price_snapshot(
                eth, Price(amount=Decimal("1810"), currency="USD"), Timestamp(dt=datetime.now(UTC))
            )
            await live_storage.save_order(_make_order(symbol="ETH/USD", price="1790"))
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert "vs mkt" in resp.text  # delta column header
                assert "+1.12%" in resp.text  # (1810-1790)/1790*100, signed
        finally:
            await observe.close()


# --------------------------------------------------------------------- #
# /status/recent-fills.json (fill toasts)                              #
# --------------------------------------------------------------------- #


class TestRecentFillsJson:
    def test_anonymous_redirects(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/status/recent-fills.json")
            assert resp.status_code == 302

    @pytest.mark.asyncio
    async def test_returns_recent_fills(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        await live_storage.save_trade(_make_trade(side="sell"))
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/status/recent-fills.json")
            assert resp.status_code == 200
            fills = resp.json()["fills"]
            assert len(fills) == 1
            assert fills[0]["symbol"] == "BTC/USD"
            assert fills[0]["side"] == "sell"
            assert "id" in fills[0] and "price" in fills[0]

    def test_no_live_db_empty(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/status/recent-fills.json")
            assert resp.status_code == 200
            assert resp.json() == {"fills": []}


# --------------------------------------------------------------------- #
# /dashboard                                                            #
# --------------------------------------------------------------------- #


class TestDashboardRoute:
    def test_anonymous_redirects_to_login(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/dashboard")
            assert resp.status_code == 302
            assert resp.headers["location"] == "/auth/login"

    def test_no_live_db_renders_unwired_card(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "unset" in resp.text.lower()
            # Emergency stop button lives in-flow below the status
            # card (Stage 8.4.E soak Day 4 — the wrapping card was
            # stripped; the button IS the affordance). All-caps
            # label is the operator's preferred styling.
            assert "EMERGENCY STOP" in resp.text

    def test_authenticated_with_empty_live_renders(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            # Stage 8.4.E: title restructured to "Trading Status" + LIVE badge.
            assert "Trading Status" in resp.text
            assert ">LIVE<" in resp.text
            # Empty state: no orders AND no trades = no symbols, so
            # the whole per-symbol body collapses to a single "no
            # active symbols yet" placeholder. The recent fills
            # section doesn't render when symbols are empty — it'd
            # be redundant ("no fills" alongside "no symbols").
            assert "No active symbols yet" in resp.text

    def test_mode_badge_defaults_to_live(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        # The mode-badge is driven by WebConfig.mode (default "live"),
        # not hardcoded — proving the same UI can render SHADOW too.
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert "mode-badge-live" in resp.text
            assert ">LIVE<" in resp.text

    def test_mode_badge_reflects_shadow_config(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        # Same templates + routes, mode="shadow" -> purple SHADOW badge.
        # This is the whole "reuse the webui for both modes" contract.
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            trading_mode="shadow",
            operator_storage=operator_storage,
            session_secret="x" * 64,
            live_storage=live_storage,
        )
        with TestClient(app, follow_redirects=False) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert "mode-badge-shadow" in resp.text
            assert ">SHADOW<" in resp.text
            assert "mode-badge-live" not in resp.text

    @pytest.mark.asyncio
    async def test_with_orders_and_trades_renders_them(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        await live_storage.save_order(_make_order(price="30100"))
        await live_storage.save_trade(_make_trade())
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "$30,100.00" in resp.text  # house usd rendering, separators included
            # Per-symbol section header carries the symbol name; the
            # aggregate "Open orders (N)" subtitle from the previous
            # layout is gone with the restructure.
            assert "BTC/USD" in resp.text
            assert "Recent Fills (Last 1)" in resp.text


# --------------------------------------------------------------------- #
# Session card (v1.1, ADR-024)                                          #
# --------------------------------------------------------------------- #


class TestSessionCapCard:
    def test_no_trip_no_banner_rendered(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "session-cap-banner" not in resp.text

    @pytest.mark.asyncio
    async def test_trip_with_no_cool_down_configured_renders_cleared(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        await live_storage.record_cap_trip(Timestamp(dt=datetime.now(UTC)), Decimal("-5.12"))
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "session-cap-cleared" in resp.text
            assert "session-cap-active" not in resp.text
            assert "-$5.12" in resp.text
            assert "cleared" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_recent_trip_within_cool_down_renders_active_warning(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        tripped_at = datetime.now(UTC) - timedelta(minutes=10)
        await live_storage.record_cap_trip(Timestamp(dt=tripped_at), Decimal("-8"))
        with _build_client(operator_storage, live_storage, cool_down_minutes=60.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "session-cap-active" in resp.text
            assert "session-cap-cleared" not in resp.text
            assert "Session-loss cap tripped" in resp.text
            assert "no new session may start" in resp.text

    @pytest.mark.asyncio
    async def test_status_card_fragment_also_renders_the_banner(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        """The HTMX fragment route shares the same template include —
        confirm the banner isn't dashboard.html-only."""
        tripped_at = datetime.now(UTC) - timedelta(minutes=10)
        await live_storage.record_cap_trip(Timestamp(dt=tripped_at), Decimal("-8"))
        with _build_client(operator_storage, live_storage, cool_down_minutes=60.0) as client:
            login_as(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert "session-cap-active" in resp.text


# --------------------------------------------------------------------- #
# /status/card fragment                                                 #
# --------------------------------------------------------------------- #


class TestStatusCardFragment:
    def test_anonymous_redirects(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/status/card")
            assert resp.status_code == 302

    def test_authenticated_returns_fragment(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert "status-card" in resp.text
            # No chrome
            assert "Sign out" not in resp.text

    def test_card_does_not_render_status_card_health_icon(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Health UX consolidated to the navbar dot 2026-05-23.

        The traffic-light icon that previously lived on the status card
        title (`id="status-health-icon"`) was removed; health now shows
        ONLY as a tiered alert dot on the navbar's heart-pulse icon
        (yellow/red overlay polled from /health/overall.json). The
        status-card fragment must NOT render the old element, and must
        NOT reference the removed /health/icon endpoint or the dead
        health-snapshot context variable.
        """
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert 'id="status-health-icon"' not in resp.text
            assert "status-card-health-icon" not in resp.text
            assert "/health/icon" not in resp.text
            # No inline health-dot span — those live on the /health page
            # itself and (as a tiered alert dot) on the navbar.
            assert "health-dot health-dot-" not in resp.text

    def test_card_uses_trading_status_with_live_badge(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Stage 8.4.E — title restructured from "Live trading status"
        to "Trading Status" + LIVE badge so the same template can
        host SHADOW later. Verifies the badge classes are present."""
        with _build_client(operator_storage, None) as client:
            login_as(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert "Trading Status" in resp.text
            assert "mode-badge mode-badge-live" in resp.text
            assert ">LIVE<" in resp.text
            # Old title gone.
            assert "Live trading status" not in resp.text


# --------------------------------------------------------------------- #
# Snapshot loader                                                       #
# --------------------------------------------------------------------- #


class TestLoadSnapshot:
    @pytest.mark.asyncio
    async def test_none_storage_returns_unwired(self) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        snap = await _load_snapshot(None, None)
        assert snap.live_wired is False
        assert snap.open_orders == ()

    @pytest.mark.asyncio
    async def test_computes_last_fill_age(self, live_storage: SQLiteStorageAdapter) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        await live_storage.save_trade(_make_trade())
        snap = await _load_snapshot(live_storage, None)
        assert snap.last_fill_age_seconds is not None
        assert snap.last_fill_age_seconds > 0

    @pytest.mark.asyncio
    async def test_empty_db_no_last_fill_age(self, live_storage: SQLiteStorageAdapter) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        snap = await _load_snapshot(live_storage, None)
        assert snap.last_fill_age_seconds is None
        assert snap.live_wired is True

    @pytest.mark.asyncio
    async def test_no_cap_trip_leaves_session_fields_empty(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        snap = await _load_snapshot(live_storage, None, cool_down_minutes=60.0)
        assert snap.last_cap_trip is None
        assert snap.last_cap_trip_age_seconds is None
        assert snap.cool_down_active is False
        assert snap.cool_down_resumes_at is None

    @pytest.mark.asyncio
    async def test_cap_trip_recorded_without_cool_down_configured_is_cleared(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        """No ``cool_down_minutes`` -> the gate is operator-disabled, so
        even a fresh trip reads as cleared, never active."""
        from wobblebot.web.routes.status import _load_snapshot

        await live_storage.record_cap_trip(Timestamp(dt=datetime.now(UTC)), Decimal("-5.12"))
        snap = await _load_snapshot(live_storage, None, cool_down_minutes=None)
        assert snap.last_cap_trip is not None
        assert snap.last_cap_trip.session_pnl_usd == Decimal("-5.12")
        assert snap.last_cap_trip_age_seconds is not None
        assert snap.last_cap_trip_age_seconds >= 0
        assert snap.cool_down_active is False
        assert snap.cool_down_resumes_at is None

    @pytest.mark.asyncio
    async def test_recent_trip_within_window_is_active(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        tripped_at = datetime.now(UTC) - timedelta(minutes=5)
        await live_storage.record_cap_trip(Timestamp(dt=tripped_at), Decimal("-8"))
        snap = await _load_snapshot(live_storage, None, cool_down_minutes=60.0)
        assert snap.cool_down_active is True
        assert snap.cool_down_resumes_at is not None
        assert snap.cool_down_resumes_at > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_old_trip_past_window_is_cleared(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.web.routes.status import _load_snapshot

        tripped_at = datetime.now(UTC) - timedelta(hours=2)
        await live_storage.record_cap_trip(Timestamp(dt=tripped_at), Decimal("-8"))
        snap = await _load_snapshot(live_storage, None, cool_down_minutes=60.0)
        assert snap.cool_down_active is False
        assert snap.last_cap_trip is not None  # the record persists; just not "active"


# --------------------------------------------------------------------- #
# Engine-state badges (ADR-030, P3 slice 3)                              #
# --------------------------------------------------------------------- #


def _engine_row(
    *,
    symbol: str = "BTC/USD",
    paused: bool = False,
    offside: bool = False,
    offside_ticks: int = 0,
    age_seconds: float = 0.0,
):  # type: ignore[no-untyped-def]
    from wobblebot.domain.engine_state import EngineStateRow

    base, quote = symbol.split("/")
    return EngineStateRow(
        symbol=Symbol(base=base, quote=quote),
        paused=paused,
        offside=offside,
        offside_ticks=offside_ticks,
        reference_price=Decimal("30000"),
        anchored_at=datetime.now(UTC) - timedelta(hours=1),
        updated_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


@pytest.mark.asyncio
class TestLoadEngineStates:
    async def test_fresh_kept_stale_dropped(self, operator_storage: SQLiteStorageAdapter) -> None:
        """The ADR's core invariant: a dead engine's rows age out. At the
        5s default tick, 3 ticks = 15s — a 2s-old row survives, a
        60s-old row vanishes."""
        from wobblebot.web.routes.status import _load_engine_states

        await operator_storage.save_engine_state(_engine_row(symbol="BTC/USD", age_seconds=2))
        await operator_storage.save_engine_state(
            _engine_row(symbol="ETH/USD", paused=True, age_seconds=60)
        )
        states = await _load_engine_states(
            operator_storage, tick_seconds=5.0, now=datetime.now(UTC)
        )
        assert set(states) == {Symbol(base="BTC", quote="USD")}

    async def test_threshold_scales_with_tick_seconds(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """An operator running a 60s tick keeps rows fresh for 180s —
        the guard measures in the WRITER's cadence, not wall-clock."""
        from wobblebot.web.routes.status import _load_engine_states

        await operator_storage.save_engine_state(_engine_row(age_seconds=60))
        states = await _load_engine_states(
            operator_storage, tick_seconds=60.0, now=datetime.now(UTC)
        )
        assert len(states) == 1

    async def test_none_storage_and_failure_degrade_to_empty(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.ports.exceptions import StorageError
        from wobblebot.web.routes.status import _load_engine_states

        assert await _load_engine_states(None, tick_seconds=5.0, now=datetime.now(UTC)) == {}

        class _Broken(SQLiteStorageAdapter):
            async def get_engine_states(self):  # type: ignore[no-untyped-def]
                raise StorageError("boom")

        broken = _Broken(":memory:")
        await broken.connect()
        try:
            assert await _load_engine_states(broken, tick_seconds=5.0, now=datetime.now(UTC)) == {}
        finally:
            await broken.close()


@pytest.mark.asyncio
class TestEngineStateBadges:
    async def test_paused_badge_renders_from_fresh_row(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(_engine_row(paused=True, age_seconds=1))
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "engine-badge-paused" in resp.text
            assert "PAUSED" in resp.text

    async def test_offside_badge_renders_with_tick_count(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(
            _engine_row(offside=True, offside_ticks=12, age_seconds=1)
        )
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert "engine-badge-offside" in resp.text
            assert "12 ticks" in resp.text

    async def test_stale_row_renders_no_badge(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """A dead engine's stale paused row must NOT keep the badge up
        — never render as confidently-anything on old data."""
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(_engine_row(paused=True, age_seconds=300))
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "engine-badge" not in resp.text

    async def test_no_rows_renders_no_badge(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """operator.db present but engine never wrote (e.g. live's
        operator_db unset): identical safe default as stale."""
        await live_storage.save_order(_make_order())
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert "engine-badge" not in resp.text


# --------------------------------------------------------------------- #
# Re-anchor banner: snooze filter + fee-only economics (P3 banner slice)#
# --------------------------------------------------------------------- #


async def _seed_drifted_grid(live_storage: SQLiteStorageAdapter) -> Order:
    """A BTC grid anchored at 30000 with one open order at the anchor.

    Paired with a current price of 30600 (2.0 spacings at 1% spacing)
    this trips the mild banner tier — drift gates, age stays zero.
    """
    from wobblebot.domain.grid import GridState

    await live_storage.save_grid_state(
        GridState(
            symbol=Symbol(base="BTC", quote="USD"),
            reference_price=Decimal("30000"),
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    return _make_order(price="30000")


@pytest.mark.asyncio
class TestReanchorBannerSnoozeAndFee:
    async def test_projected_fee_is_double_taker_on_open_notional(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        """$30 open notional -> $0.48 projected: 0.80% taker on the
        cancelled ladder plus the same again for the re-laid one
        (Kraken Tier-1 doubled 2026-07-09, ADR-038)."""
        from wobblebot.web.routes.status_reanchor import load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        recs = await load_reanchor_recommendations(
            live_storage,
            [order],
            {Symbol(base="BTC", quote="USD"): Decimal("30600")},
            {str(order.id): 0},
            set(),
            {},
        )
        assert len(recs) == 1
        assert recs[0].severity == "mild"
        assert recs[0].projected_fee_usd == Decimal("0.48")
        assert recs[0].recent_range_spacings is None  # no series -> no claim

    async def test_snoozed_symbol_suppresses_banner(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.web.routes.status_reanchor import load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        recs = await load_reanchor_recommendations(
            live_storage,
            [order],
            {Symbol(base="BTC", quote="USD"): Decimal("30600")},
            {str(order.id): 0},
            {Symbol(base="BTC", quote="USD")},
            {},
        )
        assert recs == ()

    async def test_recent_range_stat_in_spacings(self, live_storage: SQLiteStorageAdapter) -> None:
        """The activity stat: a 600-wide 2h range at 300 spacing = 2.0x.
        A fact from the sparkline series, not a probability claim."""
        from wobblebot.web.routes.status_reanchor import load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        sym = Symbol(base="BTC", quote="USD")
        recs = await load_reanchor_recommendations(
            live_storage,
            [order],
            {sym: Decimal("30600")},
            {str(order.id): 0},
            set(),
            {sym: [Decimal("30000"), Decimal("30250"), Decimal("30600")]},
        )
        assert len(recs) == 1
        assert recs[0].recent_range_spacings == pytest.approx(2.0)

    async def test_active_snooze_filters_expired_does_not(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Round-trips through the real adapter: an unexpired snooze
        suppresses, an expired one is ignored on read."""
        from wobblebot.web.routes.status_reanchor import load_reanchor_snoozes

        now = datetime.now(UTC)
        await operator_storage.save_reanchor_snooze(
            Symbol(base="BTC", quote="USD"), now + timedelta(hours=23)
        )
        await operator_storage.save_reanchor_snooze(
            Symbol(base="ETH", quote="USD"), now - timedelta(minutes=1)
        )
        active = await load_reanchor_snoozes(operator_storage, now=now)
        assert active == {Symbol(base="BTC", quote="USD")}

    async def test_resnooze_upserts_expiry(self, operator_storage: SQLiteStorageAdapter) -> None:
        """One row per symbol: a second snooze replaces the first."""
        from wobblebot.web.routes.status_reanchor import load_reanchor_snoozes

        now = datetime.now(UTC)
        sym = Symbol(base="BTC", quote="USD")
        await operator_storage.save_reanchor_snooze(sym, now - timedelta(minutes=1))
        await operator_storage.save_reanchor_snooze(sym, now + timedelta(hours=24))
        assert await load_reanchor_snoozes(operator_storage, now=now) == {sym}
        snoozes = await operator_storage.get_reanchor_snoozes()
        assert len(snoozes) == 1

    async def test_snooze_lookup_degrades_to_show_all(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Unwired operator.db or a storage failure shows every banner
        — the failure mode is a reappearing banner, never a hidden one."""
        from wobblebot.ports.exceptions import StorageError
        from wobblebot.web.routes.status_reanchor import load_reanchor_snoozes

        now = datetime.now(UTC)
        assert await load_reanchor_snoozes(None, now=now) == set()

        class _Broken(SQLiteStorageAdapter):
            async def get_reanchor_snoozes(self):  # type: ignore[no-untyped-def]
                raise StorageError("boom")

        broken = _Broken(":memory:")
        await broken.connect()
        try:
            assert await load_reanchor_snoozes(broken, now=now) == set()
        finally:
            await broken.close()

    async def test_banner_renders_fee_line_and_action_buttons(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """Full pipeline render: the banner carries the fee line, the
        firewall-bound Re-anchor form, and the UI-local Snooze form."""
        order = await _seed_drifted_grid(live_storage)
        await live_storage.save_order(order)
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            await observe.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30600"), currency="USD"),
                Timestamp(dt=datetime.now(UTC)),
            )
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert "reanchor-banner" in resp.text
                # Case-insensitive: this asserts the fee line is PRESENT,
                # not how it's cased — label styling is a design call and
                # shouldn't break a behavioral test.
                assert "projected cost" in resp.text.lower()
                assert 'action="/commands/reanchor"' in resp.text
                assert 'action="/commands/snooze-reanchor"' in resp.text
        finally:
            await observe.close()

    async def test_snoozed_banner_absent_from_render(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The end-to-end suppress: snooze row in operator.db hides the
        banner the previous test proved renders."""
        order = await _seed_drifted_grid(live_storage)
        await live_storage.save_order(order)
        await operator_storage.save_reanchor_snooze(
            Symbol(base="BTC", quote="USD"), datetime.now(UTC) + timedelta(hours=24)
        )
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            await observe.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30600"), currency="USD"),
                Timestamp(dt=datetime.now(UTC)),
            )
            app = create_app(
                config=WebConfig(bcrypt_cost=10),
                operator_storage=operator_storage,
                session_secret="x" * 64,
                live_storage=live_storage,
                observe_storage=observe,
            )
            with TestClient(app, follow_redirects=False) as client:
                login_as(client)
                resp = client.get("/dashboard")
                assert resp.status_code == 200
                assert "reanchor-banner" not in resp.text
        finally:
            await observe.close()


# --------------------------------------------------------------------- #
# State-aware pause/resume buttons (P3 blueprint: one icon, safe default)#
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestStateAwareButtons:
    """Exactly ONE of pause/resume renders per symbol, from FRESH
    engine_state only. The asymmetric safe default: absent or stale
    state shows PAUSE — an idempotent no-op if already paused —
    never RESUME, which could unknowingly restart trading."""

    async def test_paused_symbol_renders_resume_only_and_dims(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(_engine_row(paused=True, age_seconds=2))
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert resp.status_code == 200
            assert 'action="/commands/resume"' in resp.text
            assert 'action="/commands/pause"' not in resp.text
            assert "symbol-paused" in resp.text

    async def test_active_symbol_renders_pause_only(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(_engine_row(paused=False, age_seconds=2))
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert 'action="/commands/pause"' in resp.text
            assert 'action="/commands/resume"' not in resp.text
            assert "symbol-paused" not in resp.text

    async def test_absent_state_safe_defaults_to_pause(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_order(_make_order())
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert 'action="/commands/pause"' in resp.text
            assert 'action="/commands/resume"' not in resp.text
            assert "symbol-paused" not in resp.text

    async def test_stale_paused_state_safe_defaults_to_pause(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The ADR-030 invariant extended to actions: a dead engine's
        old 'paused' claim must not keep offering resume."""
        await live_storage.save_order(_make_order())
        await operator_storage.save_engine_state(_engine_row(paused=True, age_seconds=300))
        with _build_client(operator_storage, live_storage, live_tick_seconds=5.0) as client:
            login_as(client)
            resp = client.get("/dashboard")
            assert 'action="/commands/pause"' in resp.text
            assert 'action="/commands/resume"' not in resp.text
            assert "symbol-paused" not in resp.text


class TestCycleAnnotations:
    """Recent Cycles flags realization-day vs earning-day (P3 slice 20)."""

    def _long_hold_fallback_pair(self) -> tuple[Trade, Trade]:
        """The 2026-05-26 soak shape: 3-day hold, sizes don't match."""
        sell_at = datetime.now(UTC) - timedelta(minutes=1)
        buy_at = sell_at - timedelta(days=3)
        buy = Trade(
            id="TXID-old-buy",
            order_id="OID-old-buy",
            symbol=Symbol(base="BTC", quote="USD"),
            side="buy",  # type: ignore[arg-type]
            price=Price(amount=Decimal("74568.30"), currency="USD"),
            amount=Amount(value=Decimal("0.00013410"), asset="BTC"),
            fee=Decimal("0.025"),
            cost=Decimal("10.00"),
            executed_at=Timestamp(dt=buy_at),
        )
        sell = Trade(
            id="TXID-late-sell",
            order_id="OID-late-sell",
            symbol=Symbol(base="BTC", quote="USD"),
            side="sell",  # type: ignore[arg-type]
            price=Price(amount=Decimal("77643.30"), currency="USD"),
            amount=Amount(value=Decimal("0.00012879"), asset="BTC"),
            fee=Decimal("0.025"),
            cost=Decimal("10.00"),
            executed_at=Timestamp(dt=sell_at),
        )
        return buy, sell

    @pytest.mark.asyncio
    async def test_long_hold_fallback_cycle_is_annotated(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        for trade in self._long_hold_fallback_pair():
            await live_storage.save_trade(trade)
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "Recent Cycles" in body
        assert "cycle-flag" in body
        assert "held 3d 0h" in body
        assert ">inferred<" in body

    @pytest.mark.asyncio
    async def test_normal_cycle_carries_no_flags(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """A same-day, same-size cycle must stay visually quiet."""
        for trade in _make_cycle_trades():
            await live_storage.save_trade(trade)
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        # Assert the table actually rendered first — otherwise an empty
        # body would satisfy the negative assertion for free.
        assert "Recent Cycles (Last 1)" in body
        assert "cycle-flag" not in body


class TestHeldInventory:
    """Per-symbol held inventory (P3 slice 21)."""

    def test_splits_balances_by_symbol_and_values_them(self) -> None:
        balances = [
            Balance(
                asset="USD", total=Decimal("100"), available=Decimal("80"), locked=Decimal("20")
            ),
            Balance(
                asset="BTC", total=Decimal("0.001"), available=Decimal("0.001"), locked=Decimal("0")
            ),
            Balance(
                asset="ETH", total=Decimal("0.5"), available=Decimal("0.5"), locked=Decimal("0")
            ),
        ]
        prices = {
            Symbol(base="BTC", quote="USD"): Decimal("50000"),
            Symbol(base="ETH", quote="USD"): Decimal("2000"),
        }
        held = held_by_symbol(balances, prices)
        assert set(held) == {Symbol(base="BTC", quote="USD"), Symbol(base="ETH", quote="USD")}
        assert held[Symbol(base="BTC", quote="USD")].value_usd == Decimal("50")
        assert held[Symbol(base="ETH", quote="USD")].value_usd == Decimal("1000")

    def test_unpriced_holding_is_listed_without_a_valuation(self) -> None:
        """The position is real even when the price isn't known — showing
        the amount beats pretending the holding isn't there."""
        balances = [
            Balance(
                asset="DOGE", total=Decimal("500"), available=Decimal("500"), locked=Decimal("0")
            ),
        ]
        held = held_by_symbol(balances, {})
        entry = held[Symbol(base="DOGE", quote="USD")]
        assert entry.amount == Decimal("500")
        assert entry.value_usd is None

    def test_usd_and_zero_balances_are_excluded(self) -> None:
        balances = [
            Balance(
                asset="USD", total=Decimal("100"), available=Decimal("100"), locked=Decimal("0")
            ),
            Balance(asset="SOL", total=Decimal("0"), available=Decimal("0"), locked=Decimal("0")),
        ]
        assert held_by_symbol(balances, {}) == {}

    def test_per_symbol_rows_sum_to_the_scoreboard_total(self) -> None:
        """The whole point of sharing one rule: a card row can never
        disagree with the "in positions" figure above it."""
        balances = [
            Balance(
                asset="USD", total=Decimal("100"), available=Decimal("80"), locked=Decimal("20")
            ),
            Balance(
                asset="BTC", total=Decimal("0.001"), available=Decimal("0.001"), locked=Decimal("0")
            ),
            Balance(
                asset="ETH", total=Decimal("0.5"), available=Decimal("0.5"), locked=Decimal("0")
            ),
            # Unpriced — excluded from BOTH sides, so they still agree.
            Balance(
                asset="DOGE", total=Decimal("500"), available=Decimal("500"), locked=Decimal("0")
            ),
        ]
        prices = {
            Symbol(base="BTC", quote="USD"): Decimal("50000"),
            Symbol(base="ETH", quote="USD"): Decimal("2000"),
        }
        _, _, held_total = _compute_balance_metrics(balances, prices)
        per_symbol = sum(
            (h.value_usd for h in held_by_symbol(balances, prices).values() if h.value_usd),
            Decimal(0),
        )
        assert per_symbol == held_total


class TestFillsSection:
    """Recent Fills freshness + summary + per-row age (P3 slice 21)."""

    @pytest.mark.asyncio
    async def test_summary_net_is_signed_cash_flow(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        buy, sell = _make_cycle_trades()
        for trade in (buy, sell):
            await live_storage.save_trade(trade)
        snap = await _load_snapshot(live_storage, None)
        assert snap.fills_summary is not None
        # SIGNED cash flow: the SELL adds (cost - fee), the BUY
        # subtracts (cost + fee). The per-row cell shows the same
        # magnitudes with an arrow for direction, so summing that
        # column verbatim would total gross churn and call it "net".
        expected = (sell.cost - sell.fee) - (buy.cost + buy.fee)
        assert snap.fills_summary.net_usd == expected
        assert snap.fills_summary.total_fees == buy.fee + sell.fee
        assert (snap.fills_summary.buys, snap.fills_summary.sells) == (1, 1)

    @pytest.mark.asyncio
    async def test_no_fills_means_no_summary(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        snap = await _load_snapshot(live_storage, None)
        assert snap.fills_summary is None

    @pytest.mark.asyncio
    async def test_age_column_and_summary_render(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        for trade in _make_cycle_trades():
            await live_storage.save_trade(trade)
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "fills-summary" in body
        assert "1 buy / 1 sell" in body
        assert '<th class="num">age</th>' in body
        # And the freshness line left card-meta for the fills section.
        assert body.index("fills-summary") > body.index("card-meta")

    @pytest.mark.asyncio
    async def test_every_rendered_fill_has_an_age(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        for trade in _make_cycle_trades():
            await live_storage.save_trade(trade)
        snap = await _load_snapshot(live_storage, None)
        assert {t.id for t in snap.recent_trades} == set(snap.trade_ages)

    @pytest.mark.asyncio
    async def test_buy_only_window_nets_negative(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """Regression for the bug this slice's own preview caught: an
        unsigned sum reported a buy-only window as a large POSITIVE
        "net", which reads like profit. Buying spends USD."""
        await live_storage.save_trade(_make_trade(side="buy"))
        snap = await _load_snapshot(live_storage, None)
        assert snap.fills_summary is not None
        assert snap.fills_summary.net_usd < 0


@pytest.mark.asyncio
class TestReanchorViabilityStat:
    """ATR-based viability annotation (P3 slice 22, the full item).

    Answers the question drift+age can't: would a correctly-placed
    grid here actually CYCLE? The 2026-08-09 case — an
    operator-executed BTC re-anchor whose fresh, correctly-positioned
    ladder then sat idle because the market wasn't oscillating a full
    spacing.
    """

    async def _seed_hourly_bars(
        self,
        observe_storage: SQLiteStorageAdapter,
        *,
        bar_range: str,
        count: int = 40,
    ) -> None:
        """`count` hourly BTC bars, each with a true range of `bar_range`.

        Seeded relative to NOW, ending an hour ago — the loader windows
        bars to ``now - _VIABILITY_LOOKBACK_DAYS``, so a fixed base date
        gives the suite an expiry: the original ``2026-08-01`` seeding
        aged out of the 14-day window at 2026-08-16T00:00 UTC and all
        three viability tests started failing on a clock tick, first
        seen as a CI-only "failure" that had nothing to do with the
        change under test.
        """
        from wobblebot.domain.value_objects import OHLCBar

        base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=count)
        span = Decimal(bar_range)
        bars = [
            OHLCBar(
                symbol=Symbol(base="BTC", quote="USD"),
                interval_minutes=60,
                opened_at=base + timedelta(hours=i),
                open=Decimal("30000"),
                high=Decimal("30000") + span,
                low=Decimal("30000"),
                close=Decimal("30000"),
                vwap=Decimal("30000"),
                volume=Decimal("1"),
                count=10,
            )
            for i in range(count)
        ]
        await observe_storage.save_ohlc_bars(bars)

    async def _recs(
        self,
        live_storage: SQLiteStorageAdapter,
        observe_storage: SQLiteStorageAdapter | None,
    ):  # type: ignore[no-untyped-def]
        from wobblebot.web.routes.status_reanchor import load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        return await load_reanchor_recommendations(
            live_storage,
            [order],
            {Symbol(base="BTC", quote="USD"): Decimal("30600")},
            {str(order.id): 0},
            set(),
            {},
            observe_storage,
        )

    async def test_lively_market_scores_near_one_spacing_per_hour(
        self, live_storage: SQLiteStorageAdapter, observe_storage: SQLiteStorageAdapter
    ) -> None:
        """Spacing is 300 (1% of 30000). A 300-wide hourly true range
        means a typical hour traverses a full spacing -> 1.0x."""
        await self._seed_hourly_bars(observe_storage, bar_range="300")
        recs = await self._recs(live_storage, observe_storage)
        assert len(recs) == 1
        assert recs[0].atr_spacings_per_hour == pytest.approx(1.0, abs=0.01)

    async def test_dead_market_scores_well_under_one(
        self, live_storage: SQLiteStorageAdapter, observe_storage: SQLiteStorageAdapter
    ) -> None:
        """The idle-ladder case: 30-wide hourly range at 300 spacing =
        0.1x, i.e. ~10 hours per fill from a perfect ladder."""
        await self._seed_hourly_bars(observe_storage, bar_range="30")
        recs = await self._recs(live_storage, observe_storage)
        assert recs[0].atr_spacings_per_hour == pytest.approx(0.1, abs=0.01)

    async def test_poor_viability_never_suppresses_the_banner(
        self, live_storage: SQLiteStorageAdapter, observe_storage: SQLiteStorageAdapter
    ) -> None:
        """Load-bearing: "not worth re-anchoring" and "the grid is fine"
        are different states. A drifted ladder in a dead market is still
        idle capital — the answer may be pause, not silence."""
        await self._seed_hourly_bars(observe_storage, bar_range="1")
        recs = await self._recs(live_storage, observe_storage)
        assert len(recs) == 1
        assert recs[0].severity == "mild"  # NOT downgraded or dropped
        assert recs[0].atr_spacings_per_hour is not None
        assert recs[0].atr_spacings_per_hour < 0.05

    async def test_thin_bar_history_degrades_to_none(
        self, live_storage: SQLiteStorageAdapter, observe_storage: SQLiteStorageAdapter
    ) -> None:
        """Fewer bars than ATR(14) needs -> no claim, banner intact."""
        await self._seed_hourly_bars(observe_storage, bar_range="300", count=3)
        recs = await self._recs(live_storage, observe_storage)
        assert len(recs) == 1
        assert recs[0].atr_spacings_per_hour is None

    async def test_unwired_observe_db_degrades_to_none(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        recs = await self._recs(live_storage, None)
        assert len(recs) == 1
        assert recs[0].atr_spacings_per_hour is None

    async def test_both_horizons_render_in_one_stat_cell(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
        observe_storage: SQLiteStorageAdapter,
    ) -> None:
        """The two activity numbers share a cell — they only mean
        something next to each other."""
        await self._seed_hourly_bars(observe_storage, bar_range="30")
        await _seed_drifted_grid(live_storage)
        order = _make_order(price="30000")
        await live_storage.save_order(order)
        for i in range(6):
            await observe_storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30600"), currency="USD"),
                Timestamp(dt=datetime.now(UTC) - timedelta(minutes=10 - i)),
            )
        with _build_client(operator_storage, live_storage, observe=observe_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "activity: 2h · ATR/hr" in body
        assert "reanchor-stat-sep" in body


# --------------------------------------------------------------------- #
# Re-anchor banner: execution feedback (2026-08-17 defect)              #
# --------------------------------------------------------------------- #


_REANCHOR_TALLY = (
    "re-anchored BTC/USD: 30000 -> 30600; cancelled 2, " "placed 2/6 (3 refused) (1 sells deferred)"
)


def _dispatched_reanchor(
    *,
    symbol: str = "BTC/USD",
    message: str = _REANCHOR_TALLY,
    age_minutes: float = 4,
    status: str = "dispatched",
) -> PendingCommand:
    """A pending_commands row shaped like a web-button re-anchor that ran."""
    base, quote = symbol.split("/")
    executed = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return PendingCommand(
        id=uuid4(),
        command=ReanchorCommand(symbol=Symbol(base=base, quote=quote)),
        status=status,  # type: ignore[arg-type]
        channel_id="web",
        requesting_user_id="operator",
        dispatched_at=Timestamp(dt=executed),
        result=CommandResult(
            success=status == "dispatched",
            command_kind="reanchor",
            message=message,
            executed_at=Timestamp(dt=executed),
            side_effects={"symbol": symbol},
        ),
        ttl_expires_at=Timestamp(dt=executed + timedelta(minutes=5)),
        created_at=Timestamp(dt=executed - timedelta(seconds=6)),
    )


async def _seed_vetoed_reanchor_grid(live_storage: SQLiteStorageAdapter) -> Order:
    """The 2026-08-17 live incident shape: anchor AT the current price
    (a re-anchor just moved it there) but the nearest open order 2
    spacings away, because the safety layers vetoed the near levels.
    With a current price of 30600: drift 2.0 (mild), anchor distance 0.
    """
    from wobblebot.domain.grid import GridState

    await live_storage.save_grid_state(
        GridState(
            symbol=Symbol(base="BTC", quote="USD"),
            reference_price=Decimal("30600"),
            spacing_percentage=Decimal("1.0"),
            levels_above=3,
            levels_below=3,
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
    )
    return _make_order(side="sell", price="31212")


@pytest.mark.asyncio
class TestReanchorExecutionFeedback:
    """A recent executed re-anchor is visible ON the banner that
    solicited it (2026-08-17 defect: the honest result tally reached
    only the notifications bell + Discord, so a click whose near
    levels the guards vetoed looked like it "did nothing") — and when
    that result demonstrates another re-anchor cannot reduce drift,
    the recommended action switches. Annotation only: the banner's
    presence and severity never change.
    """

    async def _recs(
        self,
        live_storage: SQLiteStorageAdapter,
        operator_storage: SQLiteStorageAdapter | None,
        order: Order,
    ):  # type: ignore[no-untyped-def]
        from wobblebot.web.routes.status_reanchor import load_reanchor_recommendations

        return await load_reanchor_recommendations(
            live_storage,
            [order],
            {Symbol(base="BTC", quote="USD"): Decimal("30600")},
            {str(order.id): 0},
            set(),
            {},
            None,
            operator_storage,
        )

    async def test_recent_executed_reanchor_annotates_banner(
        self, live_storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """The engine's result message lands on the banner, verbatim."""
        order = await _seed_drifted_grid(live_storage)
        await operator_storage.save_pending_command(_dispatched_reanchor())
        recs = await self._recs(live_storage, operator_storage, order)
        assert len(recs) == 1
        assert recs[0].recent_reanchor_message == _REANCHOR_TALLY
        assert recs[0].recent_reanchor_age_seconds == pytest.approx(240, abs=30)
        # Anchor 30000 vs price 30600 = 2.0 spacings apart: a re-anchor
        # would genuinely move the ladder, so the guidance stays default.
        assert recs[0].reanchor_wont_help is False

    async def test_annotation_never_suppresses_or_downgrades(
        self, live_storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Load-bearing (module docstring invariant): execution feedback
        changes the recommendation, never the banner's tier or presence."""
        order = await _seed_drifted_grid(live_storage)
        bare = await self._recs(live_storage, operator_storage, order)
        await operator_storage.save_pending_command(_dispatched_reanchor())
        annotated = await self._recs(live_storage, operator_storage, order)
        assert len(bare) == len(annotated) == 1
        assert annotated[0].severity == bare[0].severity == "mild"
        assert annotated[0].drift_in_spacings == bare[0].drift_in_spacings

    async def test_vetoed_state_switches_guidance(
        self, live_storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Anchor at price + recent execution = another re-anchor cannot
        reduce drift; the flag flips while severity stays put."""
        order = await _seed_vetoed_reanchor_grid(live_storage)
        await operator_storage.save_pending_command(_dispatched_reanchor())
        recs = await self._recs(live_storage, operator_storage, order)
        assert len(recs) == 1
        assert recs[0].anchor_distance_spacings == pytest.approx(0.0)
        assert recs[0].reanchor_wont_help is True
        assert recs[0].severity == "mild"  # NOT downgraded
        assert recs[0].drift_in_spacings == pytest.approx(2.0)

    async def test_anchor_near_without_recent_execution_keeps_default_guidance(
        self, live_storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Anchor-near alone can be a price that wandered back — without
        a demonstrated recent attempt, re-anchor stays the recommendation
        (it would genuinely re-lay the missing near levels)."""
        order = await _seed_vetoed_reanchor_grid(live_storage)
        recs = await self._recs(live_storage, operator_storage, order)
        assert len(recs) == 1
        assert recs[0].reanchor_wont_help is False
        assert recs[0].recent_reanchor_message is None

    async def test_stale_failed_and_other_symbol_rows_do_not_annotate(
        self, live_storage: SQLiteStorageAdapter, operator_storage: SQLiteStorageAdapter
    ) -> None:
        order = await _seed_drifted_grid(live_storage)
        await operator_storage.save_pending_command(
            _dispatched_reanchor(age_minutes=61)  # outside the 1h window
        )
        await operator_storage.save_pending_command(
            _dispatched_reanchor(status="failed")  # engine refused/aborted
        )
        await operator_storage.save_pending_command(
            _dispatched_reanchor(symbol="ETH/USD")  # someone else's banner
        )
        recs = await self._recs(live_storage, operator_storage, order)
        assert len(recs) == 1
        assert recs[0].recent_reanchor_message is None
        assert recs[0].reanchor_wont_help is False

    async def test_lookup_failure_degrades_to_no_annotation(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The annotation must never break the banner it annotates —
        same posture as the viability stats."""
        from wobblebot.ports.exceptions import StorageError

        class _Broken(SQLiteStorageAdapter):
            async def get_pending_commands(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise StorageError("boom")

        order = await _seed_drifted_grid(live_storage)
        broken = _Broken(":memory:")
        await broken.connect()
        try:
            recs = await self._recs(live_storage, broken, order)
        finally:
            await broken.close()
        assert len(recs) == 1
        assert recs[0].recent_reanchor_message is None

    async def test_unwired_operator_db_degrades_to_no_annotation(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        order = await _seed_drifted_grid(live_storage)
        recs = await self._recs(live_storage, None, order)
        assert len(recs) == 1
        assert recs[0].recent_reanchor_message is None
        assert recs[0].reanchor_wont_help is False

    async def test_render_feedback_and_alternative_guidance(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
        observe_storage: SQLiteStorageAdapter,
    ) -> None:
        """Full pipeline, incident shape: the tally renders on the
        banner, the heading stops urging re-anchor, and the banner +
        severity chip + BOTH action levers survive untouched."""
        order = await _seed_vetoed_reanchor_grid(live_storage)
        await live_storage.save_order(order)
        await operator_storage.save_pending_command(_dispatched_reanchor())
        await observe_storage.save_price_snapshot(
            Symbol(base="BTC", quote="USD"),
            Price(amount=Decimal("30600"), currency="USD"),
            Timestamp(dt=datetime.now(UTC)),
        )
        with _build_client(operator_storage, live_storage, observe=observe_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "reanchor-banner" in body
        assert "reanchor-recent" in body
        assert "placed 2/6" in body  # the engine tally, verbatim
        assert "still off-grid after re-anchoring" in body
        assert "Consider re-anchoring" not in body
        assert 'reanchor-chip">mild' in body  # severity chip untouched
        # The recommendation switches; the levers never disappear.
        assert 'action="/commands/reanchor"' in body
        assert 'action="/commands/snooze-reanchor"' in body

    async def test_render_default_guidance_when_anchor_far(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
        observe_storage: SQLiteStorageAdapter,
    ) -> None:
        """Feedback without futility: price ran away again after the
        re-anchor, so the banner shows the result AND still recommends
        re-anchoring."""
        order = await _seed_drifted_grid(live_storage)
        await live_storage.save_order(order)
        await operator_storage.save_pending_command(_dispatched_reanchor())
        await observe_storage.save_price_snapshot(
            Symbol(base="BTC", quote="USD"),
            Price(amount=Decimal("30600"), currency="USD"),
            Timestamp(dt=datetime.now(UTC)),
        )
        with _build_client(operator_storage, live_storage, observe=observe_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "reanchor-recent" in body
        assert "Consider re-anchoring" in body


class TestFillFlash:
    """A newly-arrived fill lights up once, then settles (P3 slice 23)."""

    @pytest.mark.asyncio
    async def test_fresh_fill_is_flagged(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await live_storage.save_trade(_make_trade(side="buy"))  # 10s old
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "fill-fresh" in body

    @pytest.mark.asyncio
    async def test_settled_fill_is_not_flagged(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """Load-bearing: the flag is gated on AGE, not on being the top
        row. Otherwise the newest fill re-flashes every 15s poll forever
        and the highlight stops meaning "this just happened"."""
        old = _make_trade(side="buy").model_copy(
            update={"executed_at": Timestamp(dt=datetime.now(UTC) - timedelta(minutes=5))}
        )
        await live_storage.save_trade(old)
        with _build_client(operator_storage, live_storage) as client:
            login_as(client)
            body = client.get("/dashboard").text
        assert "Recent Fills (Last 1)" in body  # the row DID render
        assert "fill-fresh" not in body

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
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.status import _build_sparkline, _compute_balance_metrics

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


def _build_client(
    operator: SQLiteStorageAdapter,
    live: SQLiteStorageAdapter | None,
    *,
    cool_down_minutes: float | None = None,
    live_tick_seconds: float | None = None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        live_storage=live,
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
                assert "0.8000" in resp.text  # cycle net
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
                assert "1800.00" in resp.text  # the fetched price
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
            assert "30100" in resp.text
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
        """$30 open notional -> $0.24 projected: 0.40% taker on the
        cancelled ladder plus the same again for the re-laid one."""
        from wobblebot.web.routes.status import _load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        recs = await _load_reanchor_recommendations(
            live_storage,
            [order],
            {Symbol(base="BTC", quote="USD"): Decimal("30600")},
            {str(order.id): 0},
            set(),
            {},
        )
        assert len(recs) == 1
        assert recs[0].severity == "mild"
        assert recs[0].projected_fee_usd == Decimal("0.24")
        assert recs[0].recent_range_spacings is None  # no series -> no claim

    async def test_snoozed_symbol_suppresses_banner(
        self, live_storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.web.routes.status import _load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        recs = await _load_reanchor_recommendations(
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
        from wobblebot.web.routes.status import _load_reanchor_recommendations

        order = await _seed_drifted_grid(live_storage)
        sym = Symbol(base="BTC", quote="USD")
        recs = await _load_reanchor_recommendations(
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
        from wobblebot.web.routes.status import _load_reanchor_snoozes

        now = datetime.now(UTC)
        await operator_storage.save_reanchor_snooze(
            Symbol(base="BTC", quote="USD"), now + timedelta(hours=23)
        )
        await operator_storage.save_reanchor_snooze(
            Symbol(base="ETH", quote="USD"), now - timedelta(minutes=1)
        )
        active = await _load_reanchor_snoozes(operator_storage, now=now)
        assert active == {Symbol(base="BTC", quote="USD")}

    async def test_resnooze_upserts_expiry(self, operator_storage: SQLiteStorageAdapter) -> None:
        """One row per symbol: a second snooze replaces the first."""
        from wobblebot.web.routes.status import _load_reanchor_snoozes

        now = datetime.now(UTC)
        sym = Symbol(base="BTC", quote="USD")
        await operator_storage.save_reanchor_snooze(sym, now - timedelta(minutes=1))
        await operator_storage.save_reanchor_snooze(sym, now + timedelta(hours=24))
        assert await _load_reanchor_snoozes(operator_storage, now=now) == {sym}
        snoozes = await operator_storage.get_reanchor_snoozes()
        assert len(snoozes) == 1

    async def test_snooze_lookup_degrades_to_show_all(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Unwired operator.db or a storage failure shows every banner
        — the failure mode is a reappearing banner, never a hidden one."""
        from wobblebot.ports.exceptions import StorageError
        from wobblebot.web.routes.status import _load_reanchor_snoozes

        now = datetime.now(UTC)
        assert await _load_reanchor_snoozes(None, now=now) == set()

        class _Broken(SQLiteStorageAdapter):
            async def get_reanchor_snoozes(self):  # type: ignore[no-untyped-def]
                raise StorageError("boom")

        broken = _Broken(":memory:")
        await broken.connect()
        try:
            assert await _load_reanchor_snoozes(broken, now=now) == set()
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
                assert "Projected cost" in resp.text
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

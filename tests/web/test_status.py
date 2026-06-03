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
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        live_storage=live,
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

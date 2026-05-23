"""Tests for the status dashboard (Stage 7.2.B)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, Price, Symbol, Timestamp
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit

_TEST_USERNAME = "operator"
_TEST_PASSWORD = "hunter2"
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')


def _login(client: TestClient) -> None:
    page = client.get("/auth/login")
    token = _CSRF_RE.search(page.text)
    assert token is not None
    resp = client.post(
        "/auth/login",
        data={
            "username": _TEST_USERNAME,
            "password": _TEST_PASSWORD,
            "csrf_token": token.group("token"),
        },
    )
    assert resp.status_code == 302


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(_TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=10))
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
            _login(client)
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
            _login(client)
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

    @pytest.mark.asyncio
    async def test_with_orders_and_trades_renders_them(
        self,
        operator_storage: SQLiteStorageAdapter,
        live_storage: SQLiteStorageAdapter,
    ) -> None:
        await live_storage.save_order(_make_order(price="30100"))
        await live_storage.save_trade(_make_trade())
        with _build_client(operator_storage, live_storage) as client:
            _login(client)
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
            _login(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert "status-card" in resp.text
            # No chrome
            assert "Sign out" not in resp.text

    def test_card_renders_health_icon_inline(self, operator_storage: SQLiteStorageAdapter) -> None:
        """Stage 8.4.E follow-up 2026-05-22 — dot rendered inline.

        Verifies the dot anchor + dot span are present in the response
        body, the href targets /health, and there's NO hx-get pointing
        at /health/icon (the removed endpoint). The dot's color class
        depends on whether Kraken + daemons are wired; here the test
        client has neither, so we just check the structural plumbing.
        """
        with _build_client(operator_storage, None) as client:
            _login(client)
            resp = client.get("/status/card")
            assert resp.status_code == 200
            assert 'id="status-health-icon"' in resp.text
            assert 'href="/health"' in resp.text
            # The dot is server-rendered; no HTMX poll for it.
            assert "/health/icon" not in resp.text
            # Dot span with a color-class is in the inline render.
            assert "health-dot health-dot-" in resp.text

    def test_card_uses_trading_status_with_live_badge(
        self, operator_storage: SQLiteStorageAdapter
    ) -> None:
        """Stage 8.4.E — title restructured from "Live trading status"
        to "Trading Status" + LIVE badge so the same template can
        host SHADOW later. Verifies the badge classes are present."""
        with _build_client(operator_storage, None) as client:
            _login(client)
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

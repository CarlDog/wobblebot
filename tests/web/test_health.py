"""Tests for the Stage 8.4.E health page + dashboard icon fragment."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.services.daemon_health import DaemonHealth, DaemonStatus
from wobblebot.services.kraken_health import (
    KrakenHealthProbe,
    KrakenHealthResult,
    KrakenSystemStatus,
)
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.routes.health import OverallStatus, compute_overall_status

pytestmark = pytest.mark.unit

_TEST_USERNAME = "operator"
_TEST_PASSWORD = "hunter2"
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')


# --------------------------------------------------------------------- #
# compute_overall_status — pure roll-up math                                  #
# --------------------------------------------------------------------- #


def _kraken(status: KrakenSystemStatus) -> KrakenHealthResult:
    return KrakenHealthResult(status=status, fetched_at=datetime.now(UTC))


def _daemon(name: str, status: DaemonStatus) -> DaemonHealth:
    return DaemonHealth(
        name=name,
        label=name,
        status=status,
        last_seen=None,
        threshold_seconds=60.0,
    )


class TestComputeOverall:
    def test_all_fresh_and_online_is_green(self) -> None:
        result = compute_overall_status(
            _kraken(KrakenSystemStatus.ONLINE),
            (
                _daemon("cli/observe", DaemonStatus.FRESH),
                _daemon("cli/news", DaemonStatus.FRESH),
            ),
        )
        assert result is OverallStatus.GREEN

    def test_kraken_maintenance_is_red(self) -> None:
        result = compute_overall_status(
            _kraken(KrakenSystemStatus.MAINTENANCE),
            (_daemon("cli/observe", DaemonStatus.FRESH),),
        )
        assert result is OverallStatus.RED

    def test_kraken_maintenance_overrides_fresh_daemons(self) -> None:
        """Even with everything else green, maintenance wins."""
        result = compute_overall_status(
            _kraken(KrakenSystemStatus.MAINTENANCE),
            (
                _daemon("cli/observe", DaemonStatus.FRESH),
                _daemon("cli/news", DaemonStatus.FRESH),
                _daemon("cli/advise", DaemonStatus.FRESH),
            ),
        )
        assert result is OverallStatus.RED

    @pytest.mark.parametrize(
        "status",
        [
            KrakenSystemStatus.CANCEL_ONLY,
            KrakenSystemStatus.POST_ONLY,
            KrakenSystemStatus.PROBE_FAILED,
        ],
    )
    def test_kraken_degraded_is_yellow_even_with_fresh_daemons(
        self, status: KrakenSystemStatus
    ) -> None:
        result = compute_overall_status(
            _kraken(status),
            (_daemon("cli/observe", DaemonStatus.FRESH),),
        )
        assert result is OverallStatus.YELLOW

    def test_kraken_online_with_one_stale_daemon_is_yellow(self) -> None:
        result = compute_overall_status(
            _kraken(KrakenSystemStatus.ONLINE),
            (
                _daemon("cli/observe", DaemonStatus.FRESH),
                _daemon("cli/news", DaemonStatus.STALE),
            ),
        )
        assert result is OverallStatus.YELLOW

    def test_kraken_online_with_one_unknown_daemon_is_yellow(self) -> None:
        result = compute_overall_status(
            _kraken(KrakenSystemStatus.ONLINE),
            (_daemon("cli/observe", DaemonStatus.UNKNOWN),),
        )
        assert result is OverallStatus.YELLOW

    def test_no_kraken_probe_configured_is_yellow(self) -> None:
        """Probe is None → can't confirm Kraken → not green."""
        result = compute_overall_status(
            None,
            (_daemon("cli/observe", DaemonStatus.FRESH),),
        )
        assert result is OverallStatus.YELLOW


# --------------------------------------------------------------------- #
# Route smoke tests — auth + render                                     #
# --------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(_TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


def _stub_probe(status: KrakenSystemStatus) -> KrakenHealthProbe:
    """Build a probe whose ``get()`` returns a fixed result.

    Patches ``fetch_kraken_system_status`` via the probe's own
    ``_cached`` field so the lock-guarded ``get()`` path returns
    without hitting any client. Saves us wiring a MockTransport
    just to hand back a fixed status.
    """
    probe = KrakenHealthProbe.__new__(KrakenHealthProbe)
    probe._client = None  # type: ignore[assignment]
    probe._ttl_seconds = 60.0  # type: ignore[assignment]
    probe._timeout_seconds = 5.0  # type: ignore[assignment]
    probe._cached = KrakenHealthResult(status=status, fetched_at=datetime.now(UTC))
    import asyncio  # noqa: PLC0415 — local import to keep top imports tidy

    probe._lock = asyncio.Lock()  # type: ignore[assignment]
    return probe


@pytest.fixture
def client_with_probe(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
        kraken_health_probe=_stub_probe(KrakenSystemStatus.ONLINE),
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def client_no_probe(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=storage,
        session_secret="x" * 64,
        kraken_health_probe=None,
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


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


class TestHealthPage:
    def test_unauthenticated_redirects_to_login(self, client_with_probe: TestClient) -> None:
        resp = client_with_probe.get("/health")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

    def test_authenticated_returns_200(self, client_with_probe: TestClient) -> None:
        _login(client_with_probe)
        resp = client_with_probe.get("/health")
        assert resp.status_code == 200
        assert "Application health" in resp.text

    def test_kraken_online_renders_green(self, client_with_probe: TestClient) -> None:
        _login(client_with_probe)
        resp = client_with_probe.get("/health")
        assert "health-overall-green" in resp.text or "health-overall-yellow" in resp.text
        # Daemons aren't wired in this test, so all three are UNKNOWN
        # → overall is yellow despite Kraken being online.
        assert "health-overall-yellow" in resp.text

    def test_no_probe_renders_not_configured(self, client_no_probe: TestClient) -> None:
        _login(client_no_probe)
        resp = client_no_probe.get("/health")
        assert resp.status_code == 200
        assert "Kraken health probe is not configured" in resp.text


class TestHealthIconEndpointRemoved:
    """Stage 8.4.E follow-up 2026-05-22: /health/icon was removed.

    The dashboard dot is now rendered inline by the status card route
    so it refreshes atomically with the rest of the card body.
    Verifies the endpoint is genuinely gone — a 404, not a stale route
    silently returning the old fragment.
    """

    def test_health_icon_endpoint_returns_404(self, client_with_probe: TestClient) -> None:
        _login(client_with_probe)
        resp = client_with_probe.get("/health/icon")
        assert resp.status_code == 404


class TestKrakenMaintenanceRollsUpToRed:
    def test_maintenance_overrides_unknown_daemons(self, storage: SQLiteStorageAdapter) -> None:
        app = create_app(
            config=WebConfig(bcrypt_cost=10),
            operator_storage=storage,
            session_secret="x" * 64,
            kraken_health_probe=_stub_probe(KrakenSystemStatus.MAINTENANCE),
        )
        with TestClient(app, follow_redirects=False) as c:
            _login(c)
            resp = c.get("/health")
            assert resp.status_code == 200
            assert "health-overall-red" in resp.text

"""Tests for tools/healthcheck.py (P3 Docker HEALTHCHECK slice).

The contract under test: exit strictly 0 (healthy) or 1 (unhealthy —
Docker reserves 2, so even config/usage-adjacent failures map to 1);
daemon mode classifies through the SAME machinery /health uses;
http mode is a plain liveness GET.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from tools.healthcheck import main

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter

pytestmark = pytest.mark.unit

_EXAMPLE_CONFIG = str(
    Path(__file__).resolve().parents[1].parent / "config" / "settings.example.yml"
)


# --------------------------------------------------------------------- #
# --http mode                                                           #
# --------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler API)
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args: object) -> None:  # silence test output
        del args


@pytest.fixture
def http_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/healthz"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestHttpMode:
    def test_2xx_is_healthy(self, http_server: str) -> None:
        assert main(["--http", http_server]) == 0

    def test_5xx_is_unhealthy(self, http_server: str) -> None:
        _Handler.status = 503
        try:
            assert main(["--http", http_server]) == 1
        finally:
            _Handler.status = 200

    def test_connection_refused_is_unhealthy(self) -> None:
        # Port 9 (discard) is a safe nothing-listens target locally.
        assert main(["--http", "http://127.0.0.1:9/healthz", "--timeout", "2"]) == 1

    def test_http_error_closes_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unhealthy response still owns a resource that must be released."""
        response = BytesIO(b"unavailable")
        response.status = 503
        connection = Mock()
        connection.getresponse.return_value = response
        monkeypatch.setattr(
            "tools.healthcheck.http.client.HTTPConnection", Mock(return_value=connection)
        )
        assert main(["--http", "http://127.0.0.1:8000/healthz"]) == 1
        assert response.closed
        connection.close.assert_called_once()


# --------------------------------------------------------------------- #
# --daemon mode                                                         #
# --------------------------------------------------------------------- #


def _seed_db(db: Path, heartbeat_at: datetime | None = None, daemon: str = "cli/live") -> None:
    """Create the schema (and optionally one cli/live heartbeat) synchronously.

    The tests stay sync because ``main()`` itself calls ``asyncio.run``
    — nesting it inside a pytest-asyncio loop would RuntimeError.
    """

    async def _run() -> None:
        adapter = SQLiteStorageAdapter(db)
        await adapter.connect()
        try:
            if heartbeat_at is not None:
                await adapter.upsert_daemon_heartbeat(daemon, heartbeat_at)
        finally:
            await adapter.close()

    asyncio.run(_run())


@pytest.fixture
def operator_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp cwd shaped like the deployed layout: data/wobblebot-operator.db.

    The example config's web.operator_db default is the relative
    ``data/wobblebot-operator.db``, so chdir-ing into the temp layout
    makes the script resolve it here — no config surgery needed.
    """
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "wobblebot-operator.db"
    _seed_db(db)
    return db


class TestDaemonMode:
    def test_fresh_heartbeat_is_healthy(self, operator_db: Path) -> None:
        _seed_db(operator_db, heartbeat_at=datetime.now(UTC))
        assert main(["--daemon", "cli/live", "--config", _EXAMPLE_CONFIG]) == 0

    def test_stale_heartbeat_is_unhealthy(self, operator_db: Path) -> None:
        """A wedged-but-alive daemon (old heartbeat) must go red — the
        whole point of the HEALTHCHECK vs a running-container check."""
        _seed_db(operator_db, heartbeat_at=datetime.now(UTC) - timedelta(hours=2))
        assert main(["--daemon", "cli/live", "--config", _EXAMPLE_CONFIG]) == 1

    def test_no_heartbeat_row_is_unhealthy(self, operator_db: Path) -> None:
        """UNKNOWN counts as unhealthy — compose start_period covers
        boot; past it, never-heartbeated deserves the red."""
        del operator_db  # fixture provides the empty db + cwd
        assert main(["--daemon", "cli/live", "--config", _EXAMPLE_CONFIG]) == 1

    def test_unknown_daemon_name_is_unhealthy(self, operator_db: Path) -> None:
        del operator_db
        assert main(["--daemon", "cli/nonsense", "--config", _EXAMPLE_CONFIG]) == 1

    def test_bad_config_path_exits_one_not_two(self, tmp_path: Path) -> None:
        """Docker reserves exit code 2 — config failures are exit 1."""
        missing = tmp_path / "nope" / "settings.yml"
        assert main(["--daemon", "cli/live", "--config", str(missing)]) == 1


@pytest.mark.parametrize("age,expected", [(0, 0), (600, 1), (None, 1)])
def test_delivery_probe_requires_fresh_loop_heartbeat(operator_db, age, expected):
    if age is not None:
        _seed_db(operator_db, datetime.now(UTC) - timedelta(seconds=age), "cli/delivery")
    assert main(["--daemon", "cli/delivery", "--config", _EXAMPLE_CONFIG]) == expected


@pytest.fixture
def container_env(monkeypatch):
    for name in ("DAEMON", "URL", "CONFIG", "PROFILE"):
        monkeypatch.delenv(f"WOBBLEBOT_HEALTH_{name}", raising=False)
    return monkeypatch


def test_container_probe_uses_real_freshness_and_config(operator_db, container_env):
    container_env.setenv("WOBBLEBOT_HEALTH_DAEMON", "cli/live")
    container_env.setenv("WOBBLEBOT_HEALTH_CONFIG", _EXAMPLE_CONFIG)
    container_env.setenv("WOBBLEBOT_HEALTH_PROFILE", "cpu-only")
    _seed_db(operator_db, datetime.now(UTC))
    assert main(["--container"]) == 0
    _seed_db(operator_db, datetime.now(UTC) - timedelta(hours=2))
    assert main(["--container"]) == 1
    container_env.setenv("WOBBLEBOT_HEALTH_PROFILE", "missing-profile")
    assert main(["--container"]) == 1


def test_container_probe_uses_http_healthz(http_server, container_env):
    container_env.setenv("WOBBLEBOT_HEALTH_URL", http_server)
    assert main(["--container"]) == 0
    _Handler.status = 503
    try:
        assert main(["--container"]) == 1
    finally:
        _Handler.status = 200


def test_container_role_must_be_explicit_and_unambiguous(container_env):
    assert main(["--container"]) == 1
    container_env.setenv("WOBBLEBOT_HEALTH_DAEMON", "cli/live")
    container_env.setenv("WOBBLEBOT_HEALTH_URL", "http://127.0.0.1:8000/healthz")
    assert main(["--container"]) == 1


def test_container_invalid_http_target_is_unhealthy(container_env):
    container_env.setenv("WOBBLEBOT_HEALTH_URL", "not-a-url")
    assert main(["--container"]) == 1


@pytest.mark.parametrize(
    "argv", [[], ["--invalid"], ["--daemon"], ["--http", "x", "--timeout", "x"]]
)
def test_invalid_probe_usage_never_returns_docker_reserved_two(argv):
    assert main(argv) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/healthz",
        "http://169.254.169.254/healthz",
        "http://127.0.0.1.example.invalid/healthz",
        "file:///tmp/healthz",
        "ftp://127.0.0.1/healthz",
        "https://example.invalid/healthz",
        "http://127.0.0.1/private",
        "http://127.0.0.1/healthz?target=private",
        "http://127.0.0.1/healthz#fragment",
        "http://user:credential@127.0.0.1/healthz",
        "http://127.0.0.1:0/healthz",
        "http://127.0.0.1:65536/healthz",
        "http://2130706433/healthz",
        "http://127.0.0.1/healthz\n",
    ],
)
def test_http_probe_rejects_unintended_destinations_before_io(monkeypatch, url, capsys):
    connection = Mock()
    legacy_response = MagicMock()
    legacy_response.__enter__.return_value.status = 200
    legacy_fetch = Mock(return_value=legacy_response)
    # Both seams ensure this security regression cannot contact a rejected host
    # even when mutation-verifying the former unrestricted urllib implementation.
    monkeypatch.setattr("http.client.HTTPConnection", connection)
    monkeypatch.setattr("urllib.request.urlopen", legacy_fetch)
    assert main(["--http", url]) == 1
    connection.assert_not_called()
    legacy_fetch.assert_not_called()
    assert "credential" not in capsys.readouterr().out


def test_http_probe_ignores_environment_proxy(monkeypatch, http_server):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert main(["--http", http_server]) == 0
    assert main(["--http", http_server.replace("127.0.0.1", "localhost")]) == 0


def test_http_probe_does_not_follow_redirects():
    visited = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            visited.append(self.path)
            self.send_response(302 if self.path == "/healthz" else 200)
            self.send_header("Location", "/other-service")
            self.end_headers()

        def log_message(self, *args):
            del args

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert main(["--http", f"http://127.0.0.1:{server.server_port}/healthz"]) == 1
        assert visited == ["/healthz"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
def test_http_probe_requires_bounded_timeout(monkeypatch, timeout):
    connection = Mock()
    monkeypatch.setattr("http.client.HTTPConnection", connection)
    assert main(["--http", "http://127.0.0.1:8000/healthz", "--timeout", timeout]) == 1
    connection.assert_not_called()

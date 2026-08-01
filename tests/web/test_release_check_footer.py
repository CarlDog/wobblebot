"""Tests for the v1.1 footer "update available" indicator.

The background poller (cli/web) is exercised separately
(tests/cli/test_web.py); these tests cover the render side: the
Jinja global reads ``app.state.release_check_result`` live (like
``csrf_input``), and the footer shows/hides the indicator per the
operator-decided placement in docs/release/v1.1/operator-ux.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.services.release_checker import ReleaseCheckResult
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit


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


class TestReleaseCheckStateDefaults:
    def test_create_app_never_makes_a_network_call(self, client: TestClient) -> None:
        """create_app() itself must never poll GitHub -- only cli/web's
        real serve path spawns the background poller. Absence of a
        result (None) is the correct default."""
        assert client.app.state.release_check_result is None  # type: ignore[attr-defined]

    def test_no_update_indicator_when_result_is_none(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/dashboard")
        assert "Update available" not in resp.text


class TestFooterIndicator:
    def test_shown_when_update_available(self, client: TestClient) -> None:
        client.app.state.release_check_result = ReleaseCheckResult(  # type: ignore[attr-defined]
            update_available=True,
            current_version="1.0.0",
            latest_version="1.1.0",
            release_url="https://github.com/CarlDog/wobblebot/releases/tag/v1.1.0",
        )
        login_as(client)
        resp = client.get("/dashboard")
        assert "Update available" in resp.text
        assert "https://github.com/CarlDog/wobblebot/releases/tag/v1.1.0" in resp.text
        assert "v1.1.0" in resp.text  # tooltip text
        assert "v1.0.0" in resp.text  # tooltip's "running vX" text

    def test_hidden_when_no_update_available(self, client: TestClient) -> None:
        client.app.state.release_check_result = ReleaseCheckResult(  # type: ignore[attr-defined]
            update_available=False,
            current_version="1.0.0",
            latest_version="1.0.0",
            release_url=None,
        )
        login_as(client)
        resp = client.get("/dashboard")
        assert "Update available" not in resp.text

    def test_falls_back_to_releases_page_when_no_release_url(self, client: TestClient) -> None:
        client.app.state.release_check_result = ReleaseCheckResult(  # type: ignore[attr-defined]
            update_available=True,
            current_version="1.0.0",
            latest_version="1.1.0",
            release_url=None,
        )
        login_as(client)
        resp = client.get("/dashboard")
        assert "Update available" in resp.text
        assert "https://github.com/CarlDog/wobblebot/releases" in resp.text

    def test_indicator_appears_on_every_page_via_shared_layout(self, client: TestClient) -> None:
        """The footer lives in layout.html, included by every page --
        confirm it's not accidentally scoped to just the dashboard."""
        client.app.state.release_check_result = ReleaseCheckResult(  # type: ignore[attr-defined]
            update_available=True,
            current_version="1.0.0",
            latest_version="1.1.0",
            release_url="https://github.com/CarlDog/wobblebot/releases/tag/v1.1.0",
        )
        login_as(client)
        resp = client.get("/notifications")
        assert "Update available" in resp.text


class TestAppVersion:
    def test_app_version_matches_package_version(self, client: TestClient) -> None:
        """v1.1 fix: app.version was hardcoded '0.7.1', silently
        drifting from pyproject.toml. Now sourced from
        wobblebot.__version__ so the footer's displayed version (and
        the update-check comparison) are never stale."""
        from wobblebot import __version__

        assert client.app.version == __version__  # type: ignore[attr-defined]
        login_as(client)
        resp = client.get("/dashboard")
        assert f"WobbleBot v{__version__}" in resp.text

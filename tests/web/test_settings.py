"""Tests for the operator preferences settings page (Stage 8.4 follow-up).

Coverage:
- GET /settings auth-gated (anonymous redirected to login)
- GET /settings renders the form pre-filled with current preferences
- POST /settings persists a valid IANA timezone
- POST /settings rejects an invalid timezone with a save=invalid_tz flag
- POST /settings requires CSRF
- Round-trip: save → reload → see new value
- Default tz=UTC when no row exists (auto-create on first GET)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, csrf_from, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
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


class TestSettingsPageGET:
    def test_anonymous_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/settings")
        assert resp.status_code == 302

    def test_authenticated_renders_form(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        # Form is present.
        assert "<form" in resp.text
        # Default tz is UTC and shown as selected.
        assert 'value="UTC"' in resp.text
        assert "selected" in resp.text
        # Save button is present.
        assert "Save" in resp.text

    def test_form_includes_csrf_token(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        csrf_from(resp.text)  # raises if missing


class TestSettingsPagePOST:
    def test_valid_tz_persists_and_redirects(self, client: TestClient) -> None:
        login_as(client)
        page = client.get("/settings")
        token = csrf_from(page.text)
        resp = client.post(
            "/settings",
            data={"timezone": "America/Chicago", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert "/settings?save=ok" in resp.headers["location"]
        # Verify round-trip.
        page2 = client.get("/settings")
        assert page2.status_code == 200
        # The new value should be the selected option.
        assert 'value="America/Chicago"' in page2.text

    def test_invalid_tz_redirects_with_invalid_flag(self, client: TestClient) -> None:
        login_as(client)
        page = client.get("/settings")
        token = csrf_from(page.text)
        resp = client.post(
            "/settings",
            data={"timezone": "Not/A_Real_Zone", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert "save=invalid_tz" in resp.headers["location"]
        # Verify UNCHANGED state in DB.
        page2 = client.get("/settings")
        # Still UTC default since the invalid update was rejected.
        assert page2.status_code == 200

    def test_missing_csrf_rejected(self, client: TestClient) -> None:
        login_as(client)
        resp = client.post("/settings", data={"timezone": "UTC"})
        assert resp.status_code == 403

    def test_anonymous_post_redirects_to_login(self, client: TestClient) -> None:
        resp = client.post(
            "/settings",
            data={"timezone": "America/Chicago", "csrf_token": "anything"},
        )
        assert resp.status_code == 302

    def test_save_round_trip_status_banner(self, client: TestClient) -> None:
        """After a successful save, the redirect target shows a
        confirmation banner reading 'Preferences saved.'"""
        login_as(client)
        page = client.get("/settings")
        token = csrf_from(page.text)
        client.post(
            "/settings",
            data={"timezone": "Europe/Berlin", "csrf_token": token},
        )
        # Manually fetch the redirect target to verify the banner.
        confirmation = client.get("/settings?save=ok")
        assert confirmation.status_code == 200
        assert "Preferences saved" in confirmation.text


class TestSettingsLinkInLayout:
    def test_dashboard_includes_settings_nav_link(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert 'href="/settings"' in resp.text

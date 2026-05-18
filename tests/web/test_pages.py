"""Tests for the three stub pages — /dashboard, /cost, /audit (Stage 7.1.D).

Verifies auth gate (anonymous → 302 /auth/login), authenticated render
(layout chrome + placeholder copy), and the / → /dashboard redirect.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit


_TEST_BCRYPT_COST = 4
_TEST_USERNAME = "operator"
_TEST_PASSWORD = "hunter2"


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(_TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=_TEST_BCRYPT_COST))
    yield adapter
    await adapter.close()


@pytest.fixture
def client(storage: SQLiteStorageAdapter) -> Iterator[TestClient]:
    app = create_app(
        config=WebConfig(),
        operator_storage=storage,
        session_secret="x" * 64,
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


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


# --------------------------------------------------------------------- #
# /                                                                     #
# --------------------------------------------------------------------- #


class TestRootRedirect:
    def test_anonymous_root_redirects_to_dashboard(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

    def test_authenticated_root_redirects_to_dashboard(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"


# --------------------------------------------------------------------- #
# /dashboard, /cost, /audit — auth-gated                                #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/dashboard", "/cost", "/audit"])
class TestStubAuth:
    def test_anonymous_redirects_to_login(self, client: TestClient, path: str) -> None:
        resp = client.get(path)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"

    def test_authenticated_renders_layout(self, client: TestClient, path: str) -> None:
        _login(client)
        resp = client.get(path)
        assert resp.status_code == 200
        # Layout chrome present
        assert "WobbleBot" in resp.text
        assert 'href="/dashboard"' in resp.text
        assert 'href="/cost"' in resp.text
        assert 'href="/audit"' in resp.text
        # Logout form has CSRF token + sign-out button
        assert 'action="/auth/logout"' in resp.text
        assert "Sign out" in resp.text
        # Stub-specific placeholder copy
        assert "placeholder" in resp.text.lower()

    def test_authenticated_renders_username(self, client: TestClient, path: str) -> None:
        _login(client)
        resp = client.get(path)
        assert _TEST_USERNAME in resp.text


# --------------------------------------------------------------------- #
# Page-specific copy                                                    #
# --------------------------------------------------------------------- #


class TestStubContent:
    def test_dashboard_mentions_phase_72(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/dashboard")
        assert "Phase 7.2" in resp.text
        assert "Dashboard" in resp.text

    def test_cost_mentions_phase_72(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/cost")
        assert "Phase 7.2" in resp.text
        assert "Cost" in resp.text
        assert "LLM cost" in resp.text

    def test_audit_mentions_phase_74(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/audit")
        assert "Phase 7.4" in resp.text
        assert "Audit" in resp.text

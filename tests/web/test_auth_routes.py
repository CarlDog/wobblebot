"""Tests for the auth routes — login GET/POST + logout (Stage 7.1.C).

These exercise the full FastAPI app via ``TestClient`` against an
in-memory SQLite adapter. Per ADR-017 the seam under test is the
full request → middleware → handler → storage round-trip.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.middleware import get_or_create_csrf_token

pytestmark = pytest.mark.unit


_TEST_BCRYPT_COST = 4  # cheap hashing for fixtures; production uses 12
_TEST_PASSWORD = "hunter2"
_TEST_USERNAME = "operator"


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    """Fresh in-memory SQLite per test."""
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(_TEST_USERNAME, hash_password(_TEST_PASSWORD, cost=_TEST_BCRYPT_COST))
    yield adapter
    await adapter.close()


@pytest.fixture
def web_config() -> WebConfig:
    """Production-shaped config; rate-limit at 3 to make tests crisp."""
    return WebConfig(rate_limit_attempts=3, rate_limit_window_seconds=60)


def _attach_test_helpers(app: FastAPI) -> None:
    """Attach test-only routes that expose CSRF + session state.

    The logout-success path needs a fresh CSRF token after login,
    and the /dashboard stub that would otherwise mint one lives in
    Stage 7.1.D. Rather than couple this test file to that stage,
    we mount a tiny introspection route on the test fixture's app.
    """

    @app.get("/__test/csrf", response_class=PlainTextResponse)
    async def _csrf(request: Request) -> str:
        return get_or_create_csrf_token(request)

    @app.get("/__test/whoami", response_class=PlainTextResponse)
    async def _whoami(request: Request) -> str:
        u = request.session.get("username")
        return str(u) if isinstance(u, str) else ""


@pytest.fixture
def client(storage: SQLiteStorageAdapter, web_config: WebConfig) -> Iterator[TestClient]:
    """TestClient bound to ``create_app`` against in-memory storage."""
    app = create_app(
        config=web_config,
        operator_storage=storage,
        session_secret="x" * 64,
    )
    _attach_test_helpers(app)
    with TestClient(app, follow_redirects=False) as c:
        yield c


_CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')


def _extract_csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    assert m is not None, f"no CSRF token in HTML:\n{html[:400]}"
    return m.group("token")


def _csrf_via_test_route(client: TestClient) -> str:
    """Read the current session's CSRF token via the test helper."""
    r = client.get("/__test/csrf")
    assert r.status_code == 200
    return r.text


# --------------------------------------------------------------------- #
# GET /auth/login                                                       #
# --------------------------------------------------------------------- #


class TestLoginForm:
    def test_renders_form_with_csrf(self, client: TestClient) -> None:
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        assert "WobbleBot" in resp.text
        assert 'name="username"' in resp.text
        assert 'name="password"' in resp.text
        token = _extract_csrf(resp.text)
        assert len(token) >= 32

    def test_csrf_token_stable_across_gets_in_same_session(self, client: TestClient) -> None:
        r1 = client.get("/auth/login")
        r2 = client.get("/auth/login")
        assert _extract_csrf(r1.text) == _extract_csrf(r2.text)

    def test_already_authenticated_redirects_to_dashboard(self, client: TestClient) -> None:
        login_page = client.get("/auth/login")
        token = _extract_csrf(login_page.text)
        login_resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        assert login_resp.status_code == 302
        re_get = client.get("/auth/login")
        assert re_get.status_code == 302
        assert re_get.headers["location"] == "/dashboard"


# --------------------------------------------------------------------- #
# POST /auth/login                                                      #
# --------------------------------------------------------------------- #


class TestLoginSubmit:
    def test_valid_credentials_redirect_to_dashboard(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

    def test_valid_login_sets_session_username(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        whoami = client.get("/__test/whoami")
        assert whoami.text == _TEST_USERNAME

    def test_wrong_password_returns_401(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": "wrong",
                "csrf_token": token,
            },
        )
        assert resp.status_code == 401
        assert "Invalid username or password" in resp.text

    def test_wrong_password_leaves_session_anonymous(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": "wrong",
                "csrf_token": token,
            },
        )
        whoami = client.get("/__test/whoami")
        assert whoami.text == ""

    def test_unknown_user_returns_401(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": "ghost",
                "password": "whatever",
                "csrf_token": token,
            },
        )
        assert resp.status_code == 401
        assert "Invalid username or password" in resp.text

    def test_error_message_indistinguishable_between_wrong_user_and_wrong_pw(
        self, client: TestClient
    ) -> None:
        """Same response body regardless of which side failed — prevents
        username-enumeration via differential responses."""
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        r1 = client.post(
            "/auth/login",
            data={"username": "ghost", "password": "x", "csrf_token": token},
        )
        page2 = client.get("/auth/login")
        token2 = _extract_csrf(page2.text)
        r2 = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": "wrong",
                "csrf_token": token2,
            },
        )
        b1 = _CSRF_RE.sub('name="csrf_token" value="X"', r1.text)
        b2 = _CSRF_RE.sub('name="csrf_token" value="X"', r2.text)
        assert b1 == b2

    def test_missing_csrf_returns_403(self, client: TestClient) -> None:
        client.get("/auth/login")
        resp = client.post(
            "/auth/login",
            data={"username": _TEST_USERNAME, "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 403

    def test_mismatched_csrf_returns_403(self, client: TestClient) -> None:
        client.get("/auth/login")
        resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": "fabricated-token-value-but-wrong",
            },
        )
        assert resp.status_code == 403

    def test_empty_username_returns_422(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": "",
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        assert resp.status_code == 422

    def test_empty_password_returns_422(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": "",
                "csrf_token": token,
            },
        )
        assert resp.status_code == 422

    def test_csrf_token_rotates_after_login(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        old_token = _extract_csrf(page.text)
        client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": old_token,
            },
        )
        new_token = _csrf_via_test_route(client)
        assert new_token != old_token

    def test_stale_csrf_after_login_does_not_validate(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        old_token = _extract_csrf(page.text)
        client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": old_token,
            },
        )
        resp = client.post("/auth/logout", data={"csrf_token": old_token})
        assert resp.status_code == 403


# --------------------------------------------------------------------- #
# Rate limiting                                                         #
# --------------------------------------------------------------------- #


class TestRateLimit:
    def test_blocks_after_n_attempts(self, client: TestClient) -> None:
        for _ in range(3):
            page = client.get("/auth/login")
            token = _extract_csrf(page.text)
            r = client.post(
                "/auth/login",
                data={
                    "username": _TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": token,
                },
            )
            assert r.status_code == 401
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        r = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": "wrong",
                "csrf_token": token,
            },
        )
        assert r.status_code == 429
        assert "Too many attempts" in r.text

    def test_successful_login_resets_bucket(self, client: TestClient) -> None:
        # Two wrong attempts, then a correct one — bucket should reset.
        for _ in range(2):
            page = client.get("/auth/login")
            token = _extract_csrf(page.text)
            client.post(
                "/auth/login",
                data={
                    "username": _TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": token,
                },
            )
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        ok = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        assert ok.status_code == 302
        # Bucket reset → app.state.login_rate_limit reports 0 attempts.
        # Inspect through the live LoginRateLimit object on the app.
        limit = client.app.state.login_rate_limit
        # In TestClient, the client host is "testclient".
        import asyncio

        attempts = asyncio.get_event_loop().run_until_complete(limit.attempts_for("testclient"))
        assert attempts == 0


# --------------------------------------------------------------------- #
# POST /auth/logout                                                     #
# --------------------------------------------------------------------- #


class TestLogout:
    def _login(self, client: TestClient) -> None:
        page = client.get("/auth/login")
        token = _extract_csrf(page.text)
        resp = client.post(
            "/auth/login",
            data={
                "username": _TEST_USERNAME,
                "password": _TEST_PASSWORD,
                "csrf_token": token,
            },
        )
        assert resp.status_code == 302

    def test_logout_clears_session_and_redirects(self, client: TestClient) -> None:
        self._login(client)
        token = _csrf_via_test_route(client)
        resp = client.post("/auth/logout", data={"csrf_token": token})
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"
        whoami = client.get("/__test/whoami")
        assert whoami.text == ""

    def test_logout_without_csrf_returns_403(self, client: TestClient) -> None:
        self._login(client)
        resp = client.post("/auth/logout")
        assert resp.status_code == 403

    def test_logout_with_wrong_csrf_returns_403(self, client: TestClient) -> None:
        self._login(client)
        resp = client.post("/auth/logout", data={"csrf_token": "nope"})
        assert resp.status_code == 403

    def test_logout_rotates_csrf(self, client: TestClient) -> None:
        self._login(client)
        token_before = _csrf_via_test_route(client)
        client.post("/auth/logout", data={"csrf_token": token_before})
        token_after = _csrf_via_test_route(client)
        assert token_after != token_before

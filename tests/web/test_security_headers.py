"""Tests for the v1.1 Content-Security-Policy middleware.

Defense-in-depth over Jinja2 autoescape (ASVS L3). Verifies the header
is applied blanket-wide (every route, static assets, and 404s alike —
the reason this is real Starlette middleware rather than a per-route
dependency like CSRF/rate-limit), and that its value matches what the
templates actually need (script-src 'self' only, style-src allows
'unsafe-inline' for the inline style="..." attributes that remain).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password
from wobblebot.web.middleware import CSP_HEADER_VALUE

pytestmark = pytest.mark.unit

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "wobblebot" / "web" / "templates"
# Matches an opening <script ...> tag; a following capture group check
# below verifies it carries a src= attribute.
_SCRIPT_OPEN_TAG_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)


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


class TestCSPHeaderPresence:
    def test_anonymous_page_carries_csp(self, client: TestClient) -> None:
        resp = client.get("/auth/login")
        assert resp.headers["content-security-policy"] == CSP_HEADER_VALUE

    def test_authenticated_page_carries_csp(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/dashboard")
        assert resp.headers["content-security-policy"] == CSP_HEADER_VALUE

    def test_static_asset_carries_csp(self, client: TestClient) -> None:
        """Static files are served by a separate ASGI sub-app (StaticFiles
        mount) — must still get the header from the outer middleware."""
        resp = client.get("/static/nav.js")
        assert resp.status_code == 200
        assert resp.headers["content-security-policy"] == CSP_HEADER_VALUE

    def test_404_carries_csp(self, client: TestClient) -> None:
        """Even an error response goes through the middleware — a
        per-route dependency couldn't cover this."""
        resp = client.get("/this-route-does-not-exist")
        assert resp.status_code == 404
        assert resp.headers["content-security-policy"] == CSP_HEADER_VALUE

    def test_redirect_carries_csp(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["content-security-policy"] == CSP_HEADER_VALUE


class TestCSPHeaderValue:
    """Pin the directives the templates actually need — a regression
    here silently breaks the dashboard's own scripts/styles/fetches."""

    def test_script_src_has_no_unsafe_inline(self) -> None:
        """Every template script lives under /static/*.js now (v1.1 CSP
        fix); an 'unsafe-inline' regression here would silently permit
        injected inline scripts again."""
        directives = _parse_csp(CSP_HEADER_VALUE)
        assert directives["script-src"] == ["'self'"]

    def test_style_src_allows_self_and_unsafe_inline(self) -> None:
        """Several templates use inline style="..." (a data-driven bar
        height on the cost page, fixed <col> width hints) — style-src
        must allow it or those pages render broken, not just insecure."""
        directives = _parse_csp(CSP_HEADER_VALUE)
        assert set(directives["style-src"]) == {"'self'", "'unsafe-inline'"}

    def test_object_src_and_frame_ancestors_locked_down(self) -> None:
        directives = _parse_csp(CSP_HEADER_VALUE)
        assert directives["object-src"] == ["'none'"]
        assert directives["frame-ancestors"] == ["'none'"]

    def test_default_src_is_self_only(self) -> None:
        directives = _parse_csp(CSP_HEADER_VALUE)
        assert directives["default-src"] == ["'self'"]


class TestNoInlineScriptsRemain:
    """Static regression guard: script-src 'self' only works if no
    template ever grows a new inline <script> block. A future inline
    script wouldn't fail loudly -- it would just silently not execute
    in the browser, breaking a feature instead of erroring. Catch it
    here instead."""

    def test_every_script_tag_has_a_src_attribute(self) -> None:
        offenders: list[str] = []
        for template in _TEMPLATES_DIR.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            for match in _SCRIPT_OPEN_TAG_RE.finditer(text):
                attrs = match.group(1)
                if "src=" not in attrs:
                    offenders.append(template.name)
        assert not offenders, f"inline <script> (no src=) found in: {offenders}"


def _parse_csp(header_value: str) -> dict[str, list[str]]:
    """Split a CSP header value into {directive: [sources]}."""
    directives: dict[str, list[str]] = {}
    for part in header_value.split(";"):
        tokens = part.strip().split()
        if not tokens:
            continue
        directives[tokens[0]] = tokens[1:]
    return directives

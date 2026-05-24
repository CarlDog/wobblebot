"""Shared web-test login + CSRF helpers.

Extracted 2026-05-23 from 12 web test files that each had a
verbatim copy of:

- ``_TEST_USERNAME = "operator"`` + ``_TEST_PASSWORD = "hunter2"``
- ``_CSRF_RE = re.compile(r'name="csrf_token"\\s+value="(?P<token>[^"]+)"')``
- ``_login(client) -> None`` calling /auth/login with the CSRF token

Was audit finding #10 — drift risk was real:
``tests/web/test_auth_routes.py`` hard-coded a different bcrypt cost
than the others, which would have silently diverged if the production
bcrypt minimum changed.

Module is private (leading underscore) — only siblings under
``tests/web/`` should import. Constants stay UPPERCASE to match the
prior per-file convention; the ``login_as`` and ``csrf_from`` helpers
take the client / response as their first positional arg so calls
read naturally::

    login_as(client)
    token = csrf_from(form.text)
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

# Test credentials seeded into every web-test storage fixture. These
# are NOT production secrets — they only exist against in-memory
# SQLite. Centralized here so a credential-format change (e.g.
# minimum password length) lands once across the suite.
TEST_USERNAME = "operator"
TEST_PASSWORD = "hunter2"

# The csrf_token form field that Jinja renders into every authenticated
# POST form. Synchronizer-token middleware enforces the field's presence
# in middleware.py.
CSRF_RE = re.compile(r'name="csrf_token"\s+value="(?P<token>[^"]+)"')


def csrf_from(html: str) -> str:
    """Extract the csrf_token field value from a rendered form page.

    Raises ``AssertionError`` when no match — every authenticated form
    should carry the token; absence indicates a template or middleware
    regression worth catching loud.
    """
    match = CSRF_RE.search(html)
    assert match is not None, "csrf_token field not found in response body"
    return match.group("token")


def login_as(
    client: TestClient,
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
) -> None:
    """Log in via ``/auth/login`` and assert the 302 redirect to /dashboard.

    Default credentials match the operator+hunter2 fixture seeding in
    ``tests/web/conftest.py``-equivalents. Override only when a test
    needs to exercise alternate-user scenarios (test_auth_routes does
    this manually since it tests the auth flow itself).
    """
    page = client.get("/auth/login")
    token = csrf_from(page.text)
    resp = client.post(
        "/auth/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": token,
        },
    )
    assert resp.status_code == 302, (
        f"login expected 302 redirect; got {resp.status_code}"
    )

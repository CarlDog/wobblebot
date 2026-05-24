"""Tests for the root redirect (Stage 7.1.D).

Once Stages 7.2-7.4 land their real routes, the only thing in
``pages.py`` is the bare ``/`` redirect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, login_as
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


class TestRootRedirect:
    def test_anonymous_root_redirects_to_dashboard(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

    def test_authenticated_root_redirects_to_dashboard(self, client: TestClient) -> None:
        login_as(client)
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

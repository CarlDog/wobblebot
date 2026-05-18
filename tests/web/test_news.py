"""Tests for the /news view (Stage 7.4.A)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.cli import WebConfig
from wobblebot.domain.models import NewsItem
from wobblebot.domain.value_objects import Timestamp
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
async def news_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _item(
    *,
    source: str = "rss:coindesk",
    headline: str = "BTC up today",
    coins: list[str] | None = None,
    external_id: str | None = None,
) -> NewsItem:
    return NewsItem(
        source=source,
        external_id=external_id or f"id-{headline[:10]}",
        published_at=Timestamp(dt=datetime.now(UTC)),
        headline=headline,
        body="",
        mentioned_coins=coins or ["BTC"],
    )


def _build_client(
    operator: SQLiteStorageAdapter,
    news: SQLiteStorageAdapter | None,
) -> TestClient:
    app = create_app(
        config=WebConfig(bcrypt_cost=10),
        operator_storage=operator,
        session_secret="x" * 64,
        news_storage=news,
    )
    return TestClient(app, follow_redirects=False)


class TestNewsRoute:
    def test_anonymous_redirects(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            resp = client.get("/news")
            assert resp.status_code == 302

    def test_no_news_db_renders_unwired(self, operator_storage: SQLiteStorageAdapter) -> None:
        with _build_client(operator_storage, None) as client:
            _login(client)
            resp = client.get("/news")
            assert resp.status_code == 200
            assert "unset" in resp.text.lower()

    def test_empty_news_db_renders_placeholder(
        self,
        operator_storage: SQLiteStorageAdapter,
        news_storage: SQLiteStorageAdapter,
    ) -> None:
        with _build_client(operator_storage, news_storage) as client:
            _login(client)
            resp = client.get("/news")
            assert resp.status_code == 200
            assert "No news items" in resp.text

    @pytest.mark.asyncio
    async def test_renders_items(
        self,
        operator_storage: SQLiteStorageAdapter,
        news_storage: SQLiteStorageAdapter,
    ) -> None:
        await news_storage.save_news_item(_item(headline="BTC hits 100k", coins=["BTC"]))
        await news_storage.save_news_item(
            _item(
                source="rss:decrypt",
                headline="ETH merge complete",
                coins=["ETH"],
            )
        )
        with _build_client(operator_storage, news_storage) as client:
            _login(client)
            resp = client.get("/news")
            assert resp.status_code == 200
            assert "BTC hits 100k" in resp.text
            assert "ETH merge complete" in resp.text
            assert "rss:coindesk" in resp.text
            assert "rss:decrypt" in resp.text

    @pytest.mark.asyncio
    async def test_source_filter(
        self,
        operator_storage: SQLiteStorageAdapter,
        news_storage: SQLiteStorageAdapter,
    ) -> None:
        await news_storage.save_news_item(_item(headline="from coindesk"))
        await news_storage.save_news_item(_item(source="rss:decrypt", headline="from decrypt"))
        with _build_client(operator_storage, news_storage) as client:
            _login(client)
            resp = client.get("/news?source=rss:coindesk")
            assert resp.status_code == 200
            assert "from coindesk" in resp.text
            assert "from decrypt" not in resp.text

    @pytest.mark.asyncio
    async def test_coin_filter(
        self,
        operator_storage: SQLiteStorageAdapter,
        news_storage: SQLiteStorageAdapter,
    ) -> None:
        await news_storage.save_news_item(_item(headline="BTC story", coins=["BTC"]))
        await news_storage.save_news_item(_item(headline="ETH story", coins=["ETH"]))
        with _build_client(operator_storage, news_storage) as client:
            _login(client)
            resp = client.get("/news?coin=ETH")
            assert resp.status_code == 200
            assert "ETH story" in resp.text
            assert "BTC story" not in resp.text

    @pytest.mark.asyncio
    async def test_coin_filter_case_insensitive(
        self,
        operator_storage: SQLiteStorageAdapter,
        news_storage: SQLiteStorageAdapter,
    ) -> None:
        await news_storage.save_news_item(_item(headline="lower btc", coins=["btc"]))
        with _build_client(operator_storage, news_storage) as client:
            _login(client)
            resp = client.get("/news?coin=BTC")
            assert resp.status_code == 200
            assert "lower btc" in resp.text

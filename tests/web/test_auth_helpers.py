"""Tests for ``web.auth`` helpers — bcrypt + current_user + require_user (Stage 7.1.C)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
import pytest_asyncio
from fastapi import FastAPI

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.users import User
from wobblebot.domain.value_objects import Timestamp
from wobblebot.web.auth import (
    AuthRedirectRequired,
    current_user,
    hash_password,
    require_user,
    verify_password,
)

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


# --------------------------------------------------------------------- #
# hash_password / verify_password                                       #
# --------------------------------------------------------------------- #


class TestPasswordHashing:
    def test_hash_returns_bcrypt_prefixed_string(self) -> None:
        h = hash_password("hunter2", cost=4)  # cost=4 for fast test
        assert h.startswith("$2b$")
        assert len(h) >= 50  # bcrypt hashes are ~60 chars

    def test_hash_is_non_deterministic(self) -> None:
        h1 = hash_password("hunter2", cost=4)
        h2 = hash_password("hunter2", cost=4)
        assert h1 != h2  # different salts

    def test_hash_rejects_empty_password(self) -> None:
        with pytest.raises(ValueError):
            hash_password("", cost=4)

    def test_verify_round_trip_succeeds(self) -> None:
        h = hash_password("hunter2", cost=4)
        assert verify_password("hunter2", h) is True

    def test_verify_wrong_password_fails(self) -> None:
        h = hash_password("hunter2", cost=4)
        assert verify_password("nope", h) is False

    def test_verify_empty_password_returns_false(self) -> None:
        h = hash_password("hunter2", cost=4)
        assert verify_password("", h) is False

    def test_verify_empty_hash_returns_false(self) -> None:
        assert verify_password("hunter2", "") is False

    def test_verify_malformed_hash_returns_false(self) -> None:
        # Not a bcrypt hash; checkpw raises ValueError → we return False.
        assert verify_password("hunter2", "not-a-hash") is False

    def test_verify_case_sensitive(self) -> None:
        h = hash_password("Hunter2", cost=4)
        assert verify_password("hunter2", h) is False
        assert verify_password("Hunter2", h) is True


# --------------------------------------------------------------------- #
# current_user / require_user                                           #
# --------------------------------------------------------------------- #


class _FakeRequest:
    """Minimal Request shim — current_user only reads ``request.session``."""

    def __init__(self, session: dict[str, object]) -> None:
        self.session = session


def _make_user(username: str = "operator") -> User:
    return User(
        id=1,
        username=username,
        password_hash="$2b$04$abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


@pytest.mark.asyncio
class TestCurrentUser:
    async def test_returns_none_when_no_session_username(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        req = cast("object", _FakeRequest(session={}))
        result = await current_user(req, storage)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_none_when_session_username_not_a_string(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        req = cast("object", _FakeRequest(session={"username": 42}))
        result = await current_user(req, storage)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_none_when_username_empty_string(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        req = cast("object", _FakeRequest(session={"username": ""}))
        result = await current_user(req, storage)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_none_when_user_row_missing(self, storage: SQLiteStorageAdapter) -> None:
        # Session says "ghost" but the user was deleted / never existed.
        req = cast("object", _FakeRequest(session={"username": "ghost"}))
        result = await current_user(req, storage)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_user_when_session_resolves(self, storage: SQLiteStorageAdapter) -> None:
        await storage.create_user("operator", hash_password("hunter2", cost=4))
        req = cast("object", _FakeRequest(session={"username": "operator"}))
        result = await current_user(req, storage)  # type: ignore[arg-type]
        assert result is not None
        assert result.username == "operator"


@pytest.mark.asyncio
class TestRequireUser:
    async def test_raises_redirect_when_user_is_none(self) -> None:
        with pytest.raises(AuthRedirectRequired):
            await require_user(user=None)

    async def test_returns_user_when_present(self) -> None:
        u = _make_user()
        result = await require_user(user=u)
        assert result is u
